import paramiko


def download_logs_from_sftp():
    local_logs_dir = Path(__file__).resolve().parent.parent / "logs"
    local_logs_dir.mkdir(parents=True, exist_ok=True)

    required_sftp_vars = [
        "SFTP_HOST",
        "SFTP_USERNAME",
        "SFTP_PASSWORD",
        "SFTP_REMOTE_LOG_DIR",
    ]

    missing = [name for name in required_sftp_vars if not os.environ.get(name)]

    if missing:
        print(f"Missing SFTP environment variables: {', '.join(missing)}")
        print("Skipping SFTP log download.")
        return

    host = os.environ["SFTP_HOST"].strip()
    port = int(os.environ.get("SFTP_PORT", "22").strip())

    # Allow accidental full URL.
    host = host.replace("sftp://", "").replace("ssh://", "")

    if "@" in host:
        host = host.split("@", 1)[1]

    if "/" in host:
        host = host.split("/", 1)[0]

    if ":" in host and host.count(":") == 1:
        host_part, port_part = host.rsplit(":", 1)
        if port_part.isdigit():
            host = host_part
            port = int(port_part)

    username = os.environ["SFTP_USERNAME"]
    password = os.environ["SFTP_PASSWORD"]
    remote_log_dir = os.environ["SFTP_REMOTE_LOG_DIR"]

    print(f"Connecting to SFTP host={host!r}, port={port}, user={username!r}")
    print(f"Remote log dir={remote_log_dir!r}")

    transport = None
    sftp = None

    try:
        print("Opening SFTP transport...")
        transport = paramiko.Transport((host, port))

        # This prevents infinite stalls.
        transport.banner_timeout = 30
        transport.auth_timeout = 30
        transport.handshake_timeout = 30

        print("Authenticating SFTP...")
        transport.connect(username=username, password=password)

        print("Creating SFTP client...")
        sftp = paramiko.SFTPClient.from_transport(transport)

        print("Listing remote log directory...")
        remote_files = sftp.listdir_attr(remote_log_dir)

        print(f"Found {len(remote_files)} remote files.")

        wanted_prefixes = (
            "log-all",
            "log-CS2-SimpleAdmin",
            "log-War3CS2",
        )

        downloaded = 0
        skipped = 0
        ignored = 0

        for remote_file in remote_files:
            file_name = remote_file.filename

            if not file_name.endswith(".txt"):
                ignored += 1
                continue

            if not file_name.startswith(wanted_prefixes):
                ignored += 1
                continue

            remote_path = f"{remote_log_dir.rstrip('/')}/{file_name}"
            local_path = local_logs_dir / file_name

            if local_path.exists() and local_path.stat().st_size == remote_file.st_size:
                skipped += 1
                continue

            print(f"Downloading {file_name} ({remote_file.st_size} bytes)...")
            sftp.get(remote_path, str(local_path))
            downloaded += 1

        print("SFTP download complete.")
        print(f"Downloaded: {downloaded}")
        print(f"Skipped existing: {skipped}")
        print(f"Ignored: {ignored}")

    except Exception as e:
        print(f"SFTP download failed: {type(e).__name__}: {e}")
        raise

    finally:
        if sftp:
            print("Closing SFTP client...")
            sftp.close()

        if transport:
            print("Closing SFTP transport...")
            transport.close()

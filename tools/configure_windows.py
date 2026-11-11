"""Generate this Windows host's local configuration; no service start."""
from configure_local import main


if __name__ == "__main__":
    raise SystemExit(main("win32"))

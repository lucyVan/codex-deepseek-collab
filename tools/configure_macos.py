"""Generate this Mac's local configuration; no service start."""
from configure_local import main


if __name__ == "__main__":
    raise SystemExit(main("darwin"))

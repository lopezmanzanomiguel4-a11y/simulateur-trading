import sys

from simtrade.cli import main

if __name__ == "__main__":  # garde obligatoire : les processus du catalogue (spawn) réimportent ce module
    sys.exit(main())

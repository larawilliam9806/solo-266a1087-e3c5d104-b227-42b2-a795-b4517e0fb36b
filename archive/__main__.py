"""Run the development server: python -m archive"""

import os

from .app import create_app


def main() -> None:
    app = create_app(
        {
            "DATABASE": os.environ.get("ARCHIVE_DB", "data/archive.sqlite3"),
            "SECRET_KEY": os.environ.get("SECRET_KEY") or os.urandom(32).hex(),
        }
    )
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )


if __name__ == "__main__":
    main()

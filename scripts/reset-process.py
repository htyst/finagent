from app.config import database_path
from app.data.cli import main
path = database_path().resolve()
assert path.parent.name == "var" and path.name in {"m7-process-mock.sqlite3", "m7-process-live.sqlite3"}
main()

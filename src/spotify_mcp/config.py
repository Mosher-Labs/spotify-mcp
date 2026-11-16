"""Settings read from the environment."""

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REDIRECT_URI = 'http://127.0.0.1:8888/callback'


@dataclass(frozen=True)
class Settings:
    client_id: str
    redirect_uri: str
    data_dir: Path

    @property
    def token_path(self) -> Path:
        return self.data_dir / 'token.json'

    @property
    def log_db_path(self) -> Path:
        return self.data_dir / 'listening.sqlite3'

    @property
    def tempo_db_path(self) -> Path:
        return self.data_dir / 'tempo.sqlite3'


def load_settings() -> Settings:
    client_id = os.getenv('SPOTIFY_CLIENT_ID', '').strip()
    if not client_id:
        raise SystemExit('SPOTIFY_CLIENT_ID is not set.')
    data_dir = Path(os.getenv('SPOTIFY_MCP_DATA_DIR', '~/.spotify-mcp')).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        client_id=client_id,
        redirect_uri=os.getenv('SPOTIFY_REDIRECT_URI', DEFAULT_REDIRECT_URI).strip(),
        data_dir=data_dir,
    )

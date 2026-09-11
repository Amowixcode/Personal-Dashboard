from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "INFO"
    log_dir: str = "logs"


settings = Settings()

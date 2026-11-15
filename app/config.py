from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://localhost/almanac_grid"

    class Config:
        env_file = ".env"


settings = Settings()

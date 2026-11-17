from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://localhost/almanac_grid"
    # data.ferc.gov API key; without it, region enrichment skips FERC's
    # active hydro license / exemption lists.
    ferc_api_key: str | None = None

    class Config:
        env_file = ".env"


settings = Settings()

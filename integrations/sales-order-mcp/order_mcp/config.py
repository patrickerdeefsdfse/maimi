import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

def project_path(value: str) -> Path:
    p = Path(value).expanduser()
    return (ROOT / p).resolve() if not p.is_absolute() else p.resolve()

@dataclass
class Settings:
    data_dir: Path = ROOT / "data"
    input_roots: tuple[Path, ...] = (ROOT,)
    master_data: Path = ROOT / "config/master-data.json"
    integration_config: Path = ROOT / "config/integrations.json"
    enable_write: bool = False
    dsn: str = ""
    oracle_environment: str = "unconfigured"
    oracle_expected_dsn: str = ""
    oracle_driver_mode: str = "thin"
    db_user: str = ""
    db_password: str = field(default="", repr=False)
    user_code: str = ""
    history_price_basis: str = "unknown"
    multi_line_confirmed: bool = False
    max_bytes: int = 30 * 1024 * 1024
    max_pages: int = 30

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env", override=False)
        flag = os.getenv("ORACLE_MULTI_LINE_CONFIRMED", "false").lower()
        settings = cls(data_dir=project_path(os.getenv("ORDER_DATA_DIR", "data")),
                   input_roots=tuple(project_path(p) for p in os.getenv("ORDER_INPUT_ROOTS", ".").split(";") if p),
                   master_data=project_path(os.getenv("ORDER_MASTER_DATA", "config/master-data.json")),
                   integration_config=project_path(os.getenv("ORDER_INTEGRATIONS", "config/integrations.json")),
                   enable_write=os.getenv("ORDER_ENABLE_ORACLE_WRITE", "false").lower() == "true",
                   dsn=os.getenv("ORACLE_DSN", ""),
                   oracle_environment=os.getenv("ORACLE_ENVIRONMENT", "unconfigured"),
                   oracle_expected_dsn=os.getenv("ORACLE_EXPECTED_DSN", ""), db_user=os.getenv("ORACLE_USER", ""),
                   oracle_driver_mode=os.getenv("ORACLE_DRIVER_MODE", "thin"),
                   db_password=os.getenv("ORACLE_PASSWORD", ""), user_code=os.getenv("ORACLE_USER_CODE", ""),
                   history_price_basis=os.getenv("ORACLE_HISTORY_PRICE_BASIS", "unknown"),
                   multi_line_confirmed=flag == "true")
        if not settings.multi_line_confirmed and os.getenv("ORACLE_EBS_TEST_MULTI_LINE_CONFIRMED", "false").lower() == "true":
            from .oracle import OracleGateway
            from .oracle_test_validation import is_test_target
            settings.multi_line_confirmed = is_test_target(OracleGateway(settings).target_status())
        return settings

import ctl_fixtures
import pytest
from ctl_fixtures import INSTANCE, make_ctx

from mint_litellm.control.context import Context
from mint_litellm.control.settings_store import ensure

fresh_db = ctl_fixtures.fresh_db
litellm = ctl_fixtures.litellm
stub = ctl_fixtures.stub
login_roles = ctl_fixtures.login_roles


@pytest.fixture
def conn(db):
    """공용 스키마 위의 롤백되는 연결에 설정 행을 만든다."""
    ensure(db, INSTANCE)
    return db


@pytest.fixture
def ctx(conn) -> Context:
    return make_ctx(conn)

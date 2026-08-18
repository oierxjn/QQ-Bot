from unittest.mock import patch

import pytest

import create_tables
from src.models import Base


class FakeConnection:
    """模拟 engine.begin() 返回的连接，run_sync 直接同步执行传入函数。"""

    async def run_sync(self, fn):
        return fn(self)


class FakeBegin:
    def __init__(self):
        self.connection = FakeConnection()

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeEngine:
    def __init__(self):
        self.disposed = False

    def begin(self):
        return FakeBegin()

    async def dispose(self):
        self.disposed = True


@pytest.fixture
def run_create_tables(capsys):
    async def _run(existing_tables):
        engine = FakeEngine()
        with (
            patch("create_tables.load_database_url", return_value="postgresql+asyncpg://mock"),
            patch("create_tables.create_async_engine", return_value=engine),
            patch("create_tables.inspect") as mock_inspect,
            patch.object(create_tables.Base.metadata, "create_all") as mock_create_all,
        ):
            mock_inspect.return_value.get_table_names.return_value = list(existing_tables)
            await create_tables.create_tables()

        return engine, mock_create_all, capsys.readouterr().out

    return _run


@pytest.mark.asyncio
async def test_create_tables_calls_create_all_and_disposes_engine(run_create_tables):
    engine, mock_create_all, _ = await run_create_tables(set())

    assert mock_create_all.call_count == 1
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_create_tables_reports_all_tables_as_new_when_none_exist(run_create_tables):
    _, _, out = await run_create_tables(set())

    assert "新建的表" in out
    assert "已存在（跳过）的表" not in out
    for table in Base.metadata.tables:
        assert table in out


@pytest.mark.asyncio
async def test_create_tables_reports_all_tables_as_skipped_when_all_exist(run_create_tables):
    all_tables = set(Base.metadata.tables)
    _, mock_create_all, out = await run_create_tables(all_tables)

    # 重复执行时脚本仍会调用 create_all，真实的“不重建已有表”由 create_all 的
    # checkfirst=True 保证（该行为靠文档与 review 约束，单元测试只验证协作关系）
    assert mock_create_all.call_count == 1
    assert "已存在（跳过）的表" in out
    assert "新建的表" not in out
    for table in all_tables:
        assert table in out


@pytest.mark.asyncio
async def test_create_tables_reports_partial_new_and_skipped(run_create_tables):
    defined = set(Base.metadata.tables)
    existing = {next(iter(defined))}

    _, _, out = await run_create_tables(existing)

    assert "新建的表" in out
    assert "已存在（跳过）的表" in out

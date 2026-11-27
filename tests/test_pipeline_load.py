"""End to end without a browser: downloaded files -> --load-only -> SQLite."""

from datetime import date, timedelta

from openpyxl import Workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from app.database import Repository
from app.main import Pipeline, RunMode, RunOptions
from app.manifest import Manifest
from app.periods import Period


def _engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def _export(path, sales):
    wb = Workbook()
    wb.active.append(["Metric", "Value"])
    wb.active.append(["Sales", f"${sales:,.2f}"])
    wb.active.append(["Units", 3])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def test_load_only_loads_pending_then_nothing(settings):
    engine = _engine()
    manifest = Manifest.load(settings.manifest_file)
    days = [date(2026, 11, 20) - timedelta(days=i) for i in range(3)]
    for d in days:
        f = settings.download_dir / f"{d}.xlsx"
        _export(f, 1000 + d.day)
        manifest.mark_done(Period(d, d, "day"), f, f.stat().st_size)

    def factory():
        r = Repository(engine, "sellerboard_walmart_daily")
        r.ensure_table()
        return r

    options = RunOptions(mode=RunMode.LOAD_ONLY, date_from=date(2026, 11, 1), date_to=date(2026, 11, 20))
    summary = Pipeline(settings, options, repository_factory=factory).run()
    assert len(summary.loaded) == 3 and summary.rows_inserted == 6

    repo = factory()
    with engine.connect() as c:
        assert c.execute(select(func.count()).select_from(repo.table)).scalar() == 6
        values = c.execute(select(repo.table.c.value).where(repo.table.c.metric == "Sales",
                                                            repo.table.c.period_start == days[0])).scalars().all()
        assert [str(v) for v in values] == ["1020.0000"]

    # Second run: everything already loaded.
    summary = Pipeline(settings, options, repository_factory=factory).run()
    assert summary.loaded == []
    assert Manifest.load(settings.manifest_file).get(Period(days[0], days[0], "day"))["db_rows"] == 2

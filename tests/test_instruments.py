"""日历与合约规格（Spec §4.1 / §2.3 instruments）。"""

from __future__ import annotations

from datetime import date

from sellput.instruments import (
    NyseCalendar,
    OptionRight,
    OptionSpec,
    OptionStyle,
    Settlement,
    defaults_for_symbol,
    monthly_expiries,
    third_friday,
    underlier_kind,
)


def test_third_friday():
    assert third_friday(2025, 1) == date(2025, 1, 17)
    assert third_friday(2025, 2) == date(2025, 2, 21)
    assert third_friday(2024, 2) == date(2024, 2, 16)


def test_monthly_expiries():
    exps = monthly_expiries(date(2025, 1, 1), date(2025, 3, 31))
    assert exps == [date(2025, 1, 17), date(2025, 2, 21), date(2025, 3, 21)]


def test_symbol_defaults():
    assert defaults_for_symbol("SPX") == (OptionStyle.EUROPEAN, Settlement.CASH)
    assert defaults_for_symbol("spy") == (OptionStyle.AMERICAN, Settlement.PHYSICAL)
    assert underlier_kind("SPX") == "index"
    assert underlier_kind("SPY") == "equity"


def test_nyse_calendar():
    cal = NyseCalendar()
    jan = cal.sessions(date(2025, 1, 1), date(2025, 1, 31))
    assert date(2025, 1, 1) not in jan
    assert date(2025, 1, 2) in jan
    assert date(2025, 1, 20) not in jan  # MLK 假日
    assert not cal.is_session(date(2025, 1, 1))
    assert cal.is_session(date(2025, 1, 2))


def test_dte():
    cal = NyseCalendar()
    # 2025-01-17（周五）→ 2025-02-21（第三周五）：23 个交易日（排除 MLK / 总统日）
    assert cal.dte(date(2025, 1, 17), date(2025, 2, 21)) == 23
    assert cal.dte(date(2025, 2, 21), date(2025, 2, 21)) == 0


def test_option_spec_id():
    spec = OptionSpec("SPY", date(2025, 2, 21), 95.0, OptionRight.PUT, OptionStyle.AMERICAN)
    assert spec.option_id == (date(2025, 2, 21), 95.0, OptionRight.PUT)

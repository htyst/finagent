"""Aggregate the actual boundary assertions into the engineering check output."""
from app.planning.metrics import silent_degradation_rate
CASES=[]
def pytest_runtest_logreport(report):
    if report.when=='call' and 'synonym_matrix/test_matrix.py' in report.nodeid and 'test_metric_' not in report.nodeid:
        CASES.append({'id':report.nodeid,'claimed_success':True,'semantics_preserved':report.passed})
def pytest_terminal_summary(terminalreporter):
    if CASES:
        terminalreporter.write_line('Synonym boundary engineering checks: '+str(silent_degradation_rate(CASES)))

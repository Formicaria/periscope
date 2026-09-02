import pytest

from periscope import Severity
from periscope_prometheus.format import (
    alert_fields,
    alert_from_am,
    count_by_severity,
    format_instant_result,
    format_metric,
    format_value,
    group_targets,
    parse_range,
    severity_from_labels,
    silence_summary,
    target_fingerprint,
)


def test_parse_range():
    assert parse_range(None) == "6h"
    assert parse_range(" 30M ") == "30m"
    assert parse_range("2d") == "2d"
    for bad in ("", "6", "h", "1y", "now-6h", "6 h"):
        with pytest.raises(ValueError):
            parse_range(bad) if bad else parse_range("x")


def test_severity():
    assert severity_from_labels({"severity": "critical"}) is Severity.CRITICAL
    assert severity_from_labels({"severity": "Warning"}) is Severity.WARNING
    assert severity_from_labels({"severity": "info"}) is Severity.INFO
    assert severity_from_labels({}) is Severity.INFO


def test_alert_fields_order_and_cap():
    labels = {chr(ord("a") + i): str(i) for i in range(12)}
    labels.update({"instance": "n1:9100", "job": "node", "alertname": "X", "severity": "warning"})
    f = alert_fields(labels)
    assert list(f)[:2] == ["instance", "job"]
    assert len(f) == 8
    assert "alertname" not in f and "severity" not in f


def test_alert_from_am():
    raw = {
        "status": "firing",
        "labels": {"alertname": "HighCPU", "severity": "critical", "instance": "n1:9100", "job": "node"},
        "annotations": {"summary": "CPU is high", "description": "CPU > 90% for 5m"},
        "generatorURL": "http://prom:9090/graph?g0.expr=cpu",
        "fingerprint": "abc123",
    }
    a = alert_from_am(raw)
    assert a.fingerprint == "am:abc123"
    assert a.title == "HighCPU"
    assert a.severity is Severity.CRITICAL
    assert "CPU is high" in a.description and "CPU > 90%" in a.description
    assert a.fields["instance"] == "n1:9100" and a.fields["job"] == "node"
    assert a.url.startswith("http://prom:9090")
    e = a.to_embed("lab")
    assert e.color.value == Severity.CRITICAL.color


def test_alert_from_am_no_fingerprint():
    a = alert_from_am({"labels": {"alertname": "A", "x": "1"}, "annotations": {}})
    assert a.fingerprint == "am:alertname=A|x=1"
    assert a.url is None and a.severity is Severity.INFO


def test_format_metric_and_value():
    assert format_metric({"__name__": "up", "job": "node", "instance": "a"}) == 'up{instance="a",job="node"}'
    assert format_metric({}) == "{}"
    assert format_value("1") == "1"
    assert format_value("0.123456789") == "0.123457"
    assert format_value("NaN") == "NaN"
    assert format_value("abc") == "abc"


def test_format_instant_result_vector():
    data = {"resultType": "vector", "result": [
        {"metric": {"__name__": "up", "job": "node"}, "value": [1, "1"]},
        {"metric": {"__name__": "up", "job": "prom"}, "value": [1, "0"]},
    ]}
    text, total = format_instant_result(data)
    assert total == 2
    assert 'up{job="node"}' in text and text.splitlines()[1].startswith("0")


def test_format_instant_result_caps_rows():
    data = {"resultType": "vector", "result": [{"metric": {"i": str(i)}, "value": [1, str(i)]} for i in range(30)]}
    text, total = format_instant_result(data, max_rows=20)
    assert total == 30
    assert "10 more row(s)" in text
    assert len(text.splitlines()) == 21


def test_format_instant_result_scalar_and_empty():
    assert format_instant_result({"resultType": "scalar", "result": [1, "42"]}) == ("42", 1)
    assert format_instant_result({"resultType": "vector", "result": []}) == ("(empty result)", 0)


def test_group_targets():
    targets = [
        {"labels": {"job": "node", "instance": "a:9100"}, "health": "up"},
        {"labels": {"job": "node", "instance": "b:9100"}, "health": "down", "lastError": "timeout"},
        {"labels": {"job": "cadvisor", "instance": "c:8080"}, "health": "unknown"},
    ]
    g = group_targets(targets)
    assert list(g) == ["cadvisor", "node"]
    assert g["node"]["up"] == 1 and g["node"]["down"] == 1
    assert g["node"]["down_list"] == [("b:9100", "timeout")]
    assert g["cadvisor"]["unknown"] == 1
    assert target_fingerprint("node", "b:9100") == "prom:target:node:b:9100:down"


def test_count_by_severity():
    alerts = [{"labels": {"severity": "critical"}}, {"labels": {"severity": "warning"}}, {"labels": {}}]
    assert count_by_severity(alerts) == {"critical": 1, "warning": 1, "info": 1}


def test_silence_summary():
    s = {"id": "s1", "matchers": [{"name": "alertname", "value": "X", "isRegex": False}],
         "endsAt": "2026-09-01T12:00:00.000Z", "createdBy": "me", "comment": "test"}
    out = silence_summary(s)
    assert "`s1`" in out and 'alertname="X"' in out and "2026-09-01 12:00" in out and "test" in out

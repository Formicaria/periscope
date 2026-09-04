"""The two transports, as the settings describe them."""

import pytest
from periscope import env_scope

from periscope_docker.config import DEFAULT_HOST, DockerConfig

PORTAINER = {"PORTAINER_URL": "https://portainer.lan:9443/", "PORTAINER_API_KEY": "ptr_abc",
             "PORTAINER_ENDPOINT_ID": "3"}


def load(**env) -> DockerConfig:
    with env_scope({str(k): str(v) for k, v in env.items()}):
        return DockerConfig.from_env()


def test_defaults_talk_to_the_local_socket():
    cfg = load()
    assert cfg.host == DEFAULT_HOST and cfg.mode == "socket" and cfg.socket_path == DEFAULT_HOST
    assert cfg.base_url == "http://docker" and cfg.headers == {}
    assert cfg.endpoint == "the Docker socket at /var/run/docker.sock" and cfg.label == DEFAULT_HOST
    assert (cfg.poll_s, cfg.restart_loop_n, cfg.update_check_h) == (60, 3, 12)
    assert cfg.alert_on_stop is False and cfg.check_updates is False and cfg.tls_verify is False
    assert cfg.include == [] and cfg.ignore == [] and cfg.watches("anything") is True


def test_tcp_endpoints_and_tls():
    plain = load(DOCKER_HOST="tcp://10.0.0.5:2375")
    assert plain.mode == "tcp" and plain.socket_path == "" and plain.base_url == "http://10.0.0.5:2375"
    assert plain.label == "http://10.0.0.5:2375"
    # DOCKER_TLS_VERIFY is what turns a tcp:// endpoint into https://, the way the docker CLI reads it
    secure = load(DOCKER_HOST="tcp://docker.lan:2376", DOCKER_TLS_VERIFY="true")
    assert secure.base_url == "https://docker.lan:2376" and secure.tls_verify is True
    assert load(DOCKER_HOST="https://docker.lan:2376/").base_url == "https://docker.lan:2376"
    assert load(DOCKER_HOST="unix:///run/user/1000/docker.sock").socket_path == "/run/user/1000/docker.sock"


def test_cert_path_may_name_the_docker_directory(tmp_path):
    (tmp_path / "ca.pem").write_text("ca")
    (tmp_path / "cert.pem").write_text("cert")
    (tmp_path / "key.pem").write_text("key")
    cfg = load(DOCKER_HOST="tcp://docker.lan:2376", DOCKER_CERT_PATH=str(tmp_path))
    assert cfg.cert_path == str(tmp_path / "cert.pem") and cfg.key_path == str(tmp_path / "key.pem")
    assert cfg.ca_path == str(tmp_path / "ca.pem")
    assert cfg.base_url == "https://docker.lan:2376"          # a client certificate implies TLS
    # a file keeps its meaning, and a hand-set key wins over the directory's
    one = load(DOCKER_CERT_PATH=str(tmp_path / "cert.pem"), DOCKER_KEY_PATH="/etc/other.pem")
    assert one.cert_path.endswith("cert.pem") and one.key_path == "/etc/other.pem" and one.ca_path == ""


def test_portainer_takes_over_when_docker_host_is_left_alone():
    cfg = load(**PORTAINER)
    assert cfg.via_portainer and cfg.mode == "portainer" and cfg.socket_path == ""
    assert cfg.base_url == "https://portainer.lan:9443/api/endpoints/3/docker"
    assert cfg.headers == {"X-API-Key": "ptr_abc"}
    assert cfg.endpoint == "Portainer environment 3 at https://portainer.lan:9443"
    # an explicitly configured DOCKER_HOST is tried first, so Portainer stands down
    both = load(DOCKER_HOST="tcp://10.0.0.5:2375", **PORTAINER)
    assert not both.via_portainer and both.base_url == "http://10.0.0.5:2375"


def test_bad_settings_say_which_one():
    with pytest.raises(RuntimeError, match="PORTAINER_API_KEY"):
        load(PORTAINER_URL="https://portainer.lan")
    with pytest.raises(RuntimeError, match="PORTAINER_URL must start"):
        load(PORTAINER_URL="portainer.lan", PORTAINER_API_KEY="k")
    with pytest.raises(RuntimeError, match="DOCKER_HOST must be"):
        load(DOCKER_HOST="10.0.0.5:2375")
    with pytest.raises(RuntimeError, match="named pipes"):
        load(DOCKER_HOST="npipe:////./pipe/docker_engine")
    with pytest.raises(RuntimeError, match="DOCKER_KEY_PATH"):
        load(DOCKER_CERT_PATH="/etc/cert.pem")
    with pytest.raises(RuntimeError, match="DOCKER_POLL_S"):
        load(DOCKER_POLL_S="2")
    with pytest.raises(RuntimeError, match="DOCKER_RESTART_LOOP_N"):
        load(DOCKER_RESTART_LOOP_N="1")


def test_include_and_ignore_are_glob_lists():
    cfg = load(DOCKER_INCLUDE="jellyfin, *arr", DOCKER_IGNORE="sonarr")
    assert cfg.include == ["jellyfin", "*arr"] and cfg.ignore == ["sonarr"]
    assert cfg.watches("jellyfin") and cfg.watches("radarr")
    assert not cfg.watches("sonarr") and not cfg.watches("traefik")

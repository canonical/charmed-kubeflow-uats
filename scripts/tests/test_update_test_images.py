# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import update_test_images as uti  # noqa: E402

ARGO_URL = (
    "https://raw.githubusercontent.com/canonical/argo-operators/main/"
    "charms/argo-controller/config.yaml"
)
KFP_URL = (
    "https://raw.githubusercontent.com/canonical/kfp-operators/track/2.5/"
    "charms/kfp-profile-controller/src/default-custom-images.json"
)

CONFIG = {
    "charms": {
        "argo-operators": {
            "repo": "https://github.com/canonical/argo-operators",
            "branch": "main",
            "images": [
                {
                    "file": "charms/argo-controller/config.yaml",
                    "path": "options.executor-image.default",
                    "image": "docker.io/charmedkubeflow/argoexec:old",
                }
            ],
        },
        "kfp-operators": {
            "repo": "https://github.com/canonical/kfp-operators",
            "branch": "track/2.5",
            "images": [
                {
                    "file": "charms/kfp-profile-controller/src/default-custom-images.json",
                    "path": "frontend",
                    "image": "docker.io/charmedkubeflow/frontend:1",
                },
                {
                    "file": "charms/kfp-profile-controller/src/default-custom-images.json",
                    "path": "visualization_server",
                    "image": "docker.io/charmedkubeflow/visualization-server:1",
                },
            ],
        },
    },
    "static": ["docker.io/library/busybox:1.36", "docker.io/charmedkubeflow/frontend:1"],
}

UPSTREAM = {
    ARGO_URL: "options:\n  executor-image:\n    type: string\n"
    "    default: charmedkubeflow/argoexec:new\n",
    KFP_URL: json.dumps(
        {
            "frontend": "docker.io/charmedkubeflow/frontend:1",
            "visualization_server": "docker.io/charmedkubeflow/visualization-server:2",
        }
    ),
}


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.responses:
            raise uti.ImagesConfigError(f"Failed to fetch {url}: 404")
        return self.responses[url]


@pytest.fixture
def config():
    return copy.deepcopy(CONFIG)


@pytest.fixture
def images_file(tmp_path, config):
    path = tmp_path / "test.images.yaml"
    uti.write_config(path, config)
    return path


@pytest.mark.parametrize(
    "ref, expected",
    [
        ("charmedkubeflow/metacontroller:1", "docker.io/charmedkubeflow/metacontroller:1"),
        ("busybox:1.36", "docker.io/busybox:1.36"),
        ("docker.io/library/python:3.11", "docker.io/library/python:3.11"),
        ("ghcr.io/kubeflow/katib/x:v1", "ghcr.io/kubeflow/katib/x:v1"),
        ("localhost:5000/img:1", "localhost:5000/img:1"),
        ("localhost/img:1", "localhost/img:1"),
        ('  "docker.io/a/b:1" ', "docker.io/a/b:1"),
    ],
)
def test_normalize_image(ref, expected):
    assert uti.normalize_image(ref) == expected


def test_resolve_path():
    data = {"options": {"img": {"default": "x"}}, "flat": "y"}
    assert uti.resolve_path(data, "options.img.default") == "x"
    assert uti.resolve_path(data, "flat") == "y"
    with pytest.raises(KeyError):
        uti.resolve_path(data, "options.missing.default")
    with pytest.raises(KeyError):
        uti.resolve_path(data, "flat.deeper")


def test_parse_content():
    assert uti.parse_content('{"a": 1}', "x.json") == {"a": 1}
    assert uti.parse_content("a: 1\n", "x.yaml") == {"a": 1}
    assert uti.parse_content("a: 1\n", "x.yml") == {"a": 1}
    with pytest.raises(uti.ImagesConfigError):
        uti.parse_content("a = 1", "x.toml")
    with pytest.raises(uti.ImagesConfigError):
        uti.parse_content("{", "x.json")


def test_raw_url():
    assert (
        uti.raw_url("https://github.com/canonical/kfp-operators.git", "track/2.5", "/a/b.json")
        == "https://raw.githubusercontent.com/canonical/kfp-operators/track/2.5/a/b.json"
    )
    with pytest.raises(uti.ImagesConfigError):
        uti.raw_url("https://gitlab.com/canonical/kfp-operators", "main", "a")


def test_compute_updates_and_cache(config):
    fetcher = FakeFetcher(UPSTREAM)
    changes = uti.compute_updates(config, fetcher)

    assert changes == [
        uti.Change(
            "argo-operators",
            "charms/argo-controller/config.yaml",
            "options.executor-image.default",
            "docker.io/charmedkubeflow/argoexec:old",
            "docker.io/charmedkubeflow/argoexec:new",
        ),
        uti.Change(
            "kfp-operators",
            "charms/kfp-profile-controller/src/default-custom-images.json",
            "visualization_server",
            "docker.io/charmedkubeflow/visualization-server:1",
            "docker.io/charmedkubeflow/visualization-server:2",
        ),
    ]
    # the shared kfp file is fetched only once
    assert sorted(fetcher.calls) == sorted([ARGO_URL, KFP_URL])


def test_compute_updates_missing_path(config):
    config["charms"]["kfp-operators"]["images"][0]["path"] = "nope"
    with pytest.raises(uti.ImagesConfigError, match="'nope' not found"):
        uti.compute_updates(config, FakeFetcher(UPSTREAM))


def test_compute_updates_non_string_value(config):
    config["charms"]["argo-operators"]["images"][0]["path"] = "options.executor-image"
    with pytest.raises(uti.ImagesConfigError, match="not an image reference"):
        uti.compute_updates(config, FakeFetcher(UPSTREAM))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(extra=1),
        lambda c: c.update(static="busybox"),
        lambda c: c["charms"]["argo-operators"].pop("branch"),
        lambda c: c["charms"]["argo-operators"].update(repo="not-a-url"),
        lambda c: c["charms"]["argo-operators"].update(images=[]),
        lambda c: c["charms"]["argo-operators"]["images"][0].pop("path"),
        lambda c: c["charms"]["argo-operators"]["images"][0].update(image=""),
    ],
)
def test_validate_config_errors(config, mutate):
    mutate(config)
    with pytest.raises(uti.ImagesConfigError):
        uti.validate_config(config)


def test_validate_config_ok(config):
    uti.validate_config(config)


def test_list(images_file, capsys):
    fetcher = FakeFetcher({})
    assert uti.main(["--file", str(images_file), "--list"], fetcher) == uti.EXIT_OK
    assert capsys.readouterr().out.splitlines() == [
        "docker.io/charmedkubeflow/argoexec:old",
        "docker.io/charmedkubeflow/frontend:1",
        "docker.io/charmedkubeflow/visualization-server:1",
        "docker.io/library/busybox:1.36",
    ]
    assert fetcher.calls == []


def test_check_outdated_does_not_write(images_file):
    before = images_file.read_text()
    rc = uti.main(["--file", str(images_file), "--check"], FakeFetcher(UPSTREAM))
    assert rc == uti.EXIT_OUTDATED
    assert images_file.read_text() == before


def test_dry_run_prints_diff_and_does_not_write(images_file, capsys):
    before = images_file.read_text()
    rc = uti.main(["--file", str(images_file), "--dry-run"], FakeFetcher(UPSTREAM))
    assert rc == uti.EXIT_OK
    assert images_file.read_text() == before
    out = capsys.readouterr().out
    assert "-      image: docker.io/charmedkubeflow/argoexec:old" in out
    assert "+      image: docker.io/charmedkubeflow/argoexec:new" in out
    assert "+      image: docker.io/charmedkubeflow/visualization-server:2" in out
    assert "frontend:1" not in "".join(
        line
        for line in out.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    )


def test_dry_run_up_to_date_prints_nothing(images_file, capsys):
    uti.main(["--file", str(images_file)], FakeFetcher(UPSTREAM))
    capsys.readouterr()
    rc = uti.main(["--file", str(images_file), "--dry-run"], FakeFetcher(UPSTREAM))
    assert rc == uti.EXIT_OK
    assert capsys.readouterr().out == ""


def test_exclusive_modes(images_file):
    with pytest.raises(SystemExit):
        uti.main(["--file", str(images_file), "--dry-run", "--check"], FakeFetcher(UPSTREAM))


def test_update_writes_and_preserves_order(images_file):
    assert uti.main(["--file", str(images_file)], FakeFetcher(UPSTREAM)) == uti.EXIT_OK

    text = images_file.read_text()
    updated = yaml.safe_load(text)
    expected = copy.deepcopy(CONFIG)
    expected["charms"]["argo-operators"]["images"][0][
        "image"
    ] = "docker.io/charmedkubeflow/argoexec:new"
    expected["charms"]["kfp-operators"]["images"][1][
        "image"
    ] = "docker.io/charmedkubeflow/visualization-server:2"
    assert updated == expected
    assert list(updated) == ["charms", "static"]
    assert list(updated["charms"]["argo-operators"]) == ["repo", "branch", "images"]

    # running again finds nothing to do
    rc = uti.main(["--file", str(images_file), "--check"], FakeFetcher(UPSTREAM))
    assert rc == uti.EXIT_OK


def test_fetch_error_does_not_write(images_file):
    before = images_file.read_text()
    assert uti.main(["--file", str(images_file)], FakeFetcher({})) == uti.EXIT_ERROR
    assert images_file.read_text() == before


def test_invalid_file(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a list\n")
    assert uti.main(["--file", str(path), "--list"]) == uti.EXIT_ERROR


def test_repo_file_is_valid():
    cfg = uti.load_config(uti.DEFAULT_FILE)
    assert cfg["charms"]

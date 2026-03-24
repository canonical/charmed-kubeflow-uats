# Ambient Integration Test

This test verifies that Istio Ambient Mesh properly enforces RBAC isolation between Kubeflow profiles.

The ambient integration tests are located in the `driver/ambient/` directory to keep them separate from the main UAT test suite.

## What it Tests

The test creates two separate Kubeflow profiles for isolation testing:
1. `profile1` - First test profile
2. `profile2` - Second test profile (contains the test pod)

Both profiles are created from the same template (`ambient-profile.yaml.j2`), avoiding any conflicts with existing profiles like `test-kubeflow`.

It then:
1. Creates a simple curl pod in the `profile2` namespace
2. Executes a curl command from within the pod to access the KFP API
3. Attempts to list experiments in the `profile1` namespace while impersonating `profile1@email.com` (the correct user)
4. Verifies that the request is denied with HTTP 403 and "RBAC: access denied" message
5. This proves ambient mesh is preventing header spoofing
6. Cleans up both profiles and all resources after the test

## Running the Test

### Run Only Ambient Test

Run only the ambient integration test using tox:

```bash
# Local mode (tests run in current environment)
tox -e uats-local -- --include-ambient-tests -k ambient

# Remote mode (tests run from a cloned git repo)
tox -e uats-remote -- --include-ambient-tests -k ambient
```

### Run With All Driver Tests

Include the ambient test when running all driver tests:

```bash
tox -e uats-local -- --include-ambient-tests
```

### Skip Ambient Tests (default)

By default, ambient tests are skipped unless you explicitly pass `--include-ambient-tests`:

```bash
tox -e uats-local  # ambient tests will be skipped
```

## Prerequisites

- A deployed Kubeflow cluster with Istio Ambient Mesh enabled
- Kubectl configured to access the cluster
- Python dependencies installed (pytest, lightkube, tenacity)

## Test Implementation Files

- `driver/ambient/test_ambient_integration.py` - Main test implementation
- `driver/ambient/conftest.py` - Pytest configuration and fixtures for ambient tests
- `assets/ambient-profile.yaml.j2` - Jinja2 template for creating test profiles

## Expected Behavior

When ambient mesh RBAC is working correctly:
- The curl command should return HTTP 403
- The response should contain "RBAC: access denied"
- This confirms that users in one profile cannot access resources in another profile

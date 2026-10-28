#!/bin/sh
# Run from the chart root with Helm 3, Python 3 and PyYAML installed.
set -eu

chart_version=$(python3 -c 'import yaml; print(yaml.safe_load(open("Chart.yaml"))["version"])')
if [ -n "${CI_COMMIT_TAG:-}" ] && [ "${CI_COMMIT_TAG#v}" != "$chart_version" ]; then
  echo "Release tag ${CI_COMMIT_TAG} must match Chart.yaml version ${chart_version} (optional v prefix)." >&2
  exit 1
fi

render_dir=$(mktemp -d)
trap 'rm -rf "$render_dir"' EXIT

python3 -m unittest discover -s tests -p 'test_*.py'
helm lint . --strict
helm template dune . --namespace dune --include-crds > "$render_dir/default.yaml"
python3 tests/validate-render.py "$render_dir/default.yaml" dune

helm template dune . --namespace dune-update-check --include-crds \
  --set-string images.registry=registry.example.test/update-check \
  > "$render_dir/custom.yaml"
python3 tests/validate-render.py "$render_dir/custom.yaml" dune-update-check

# Exercise the opt-in policies with an explicit trusted node address.
helm template dune . --namespace dune-update-check --include-crds \
  --set networkPolicy.enabled=true \
  --set-string 'networkPolicy.nodeCIDRs[0]=192.0.2.10/32' \
  > "$render_dir/network.yaml"
python3 tests/validate-render.py "$render_dir/network.yaml" dune-update-check

# Exercise every gameplay setting and map resource overrides before packaging.
helm template dune . --namespace dune --include-crds \
  -f tests/values-world-custom.yaml > "$render_dir/world.yaml"
python3 tests/validate-render.py "$render_dir/world.yaml" dune

# Validate the archive that will actually be published, including .helmignore.
mkdir -p dist
helm package . --destination dist
helm template dune "dist/dune-awakening-${chart_version}.tgz" \
  --namespace dune --include-crds > "$render_dir/packaged.yaml"
python3 tests/validate-render.py "$render_dir/packaged.yaml" dune
diff -u "$render_dir/default.yaml" "$render_dir/packaged.yaml"
helm template dune "dist/dune-awakening-${chart_version}.tgz" \
  --namespace dune-update-check --include-crds \
  --set networkPolicy.enabled=true \
  --set-string 'networkPolicy.nodeCIDRs[0]=192.0.2.10/32' \
  > "$render_dir/packaged-network.yaml"
diff -u "$render_dir/network.yaml" "$render_dir/packaged-network.yaml"
helm template dune "dist/dune-awakening-${chart_version}.tgz" \
  --namespace dune --include-crds -f tests/values-world-custom.yaml \
  > "$render_dir/packaged-world.yaml"
python3 tests/validate-render.py "$render_dir/packaged-world.yaml" dune
diff -u "$render_dir/world.yaml" "$render_dir/packaged-world.yaml"

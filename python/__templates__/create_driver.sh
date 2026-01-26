#!/bin/bash
set -euxv

# accepted parameters are:
# $1: driver name
# $2: driver class
# $3: author name
# $4: author email

# check if the number of parameters is correct
if [ "$#" -ne 4 ]; then
    echo "Illegal number of parameters"
    echo "Usage: create_driver.sh <driver_name> <driver_class> <author_name> <author_email>"
    echo "Example: create_driver.sh my-driver MyDriver \"John Something\" john@somewhere.com"
    exit 1
fi

export DRIVER_NAME=$1
export DRIVER_CLASS=$2
export AUTHOR_NAME=$3
export AUTHOR_EMAIL=$4

# Convert hyphens to underscores for Python module name
export DRIVER_MODULE_NAME=$(echo "${DRIVER_NAME}" | sed 's/-/_/g')

# MacOS has a different syntax for sed -i, we either use gsed (GNU sed) or apply the right -i syntax
if command -v gsed &> /dev/null; then
    sed_cmd="gsed"
elif [[ "$(uname)" == "Darwin" ]]; then
    sed_cmd="sed -i ''"
else
    sed_cmd="sed -i"
fi

# Get the script directory and navigate to python/ root
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PYTHON_ROOT="$( cd "${SCRIPT_DIR}/.." && pwd )"

# create the driver directory
DRIVER_DIRECTORY=${PYTHON_ROOT}/packages/jumpstarter-driver-${DRIVER_NAME}
MODULE_DIRECTORY=${DRIVER_DIRECTORY}/jumpstarter_driver_${DRIVER_MODULE_NAME}
# create the module directories
mkdir -p ${MODULE_DIRECTORY}
mkdir -p ${DRIVER_DIRECTORY}/examples

# Define paths
DOCS_DIRECTORY=${PYTHON_ROOT}/docs/source/reference/package-apis/drivers
DOC_FILE=${DOCS_DIRECTORY}/${DRIVER_NAME}.md
README_FILE=${DRIVER_DIRECTORY}/README.md

# Create README.md file with initial documentation
echo "Creating README.md file: ${README_FILE}"
cat > "${README_FILE}" << 'EOF'
# ${DRIVER_CLASS} Driver

`jumpstarter-driver-${DRIVER_NAME}` provides functionality for interacting with ${DRIVER_NAME} devices.

## Installation

```shell
pip3 install --extra-index-url https://pkg.jumpstarter.dev/simple/ jumpstarter-driver-${DRIVER_NAME}
```

## Configuration

Example configuration:

```yaml
export:
  ${DRIVER_NAME}:
    type: jumpstarter_driver_${DRIVER_MODULE_NAME}.driver.${DRIVER_CLASS}
    config:
      # Add required config parameters here
```

## API Reference

Add API documentation here.
EOF
# Need to expand variables after EOF to prevent early expansion
$sed_cmd "s/\${DRIVER_CLASS}/${DRIVER_CLASS}/g; s/\${DRIVER_NAME}/${DRIVER_NAME}/g; s/\${DRIVER_MODULE_NAME}/${DRIVER_MODULE_NAME}/g" "${README_FILE}"
echo "README.md file content:"
cat "${README_FILE}"

# Create symlink from documentation directory to README.md
mkdir -p ${DOCS_DIRECTORY}
echo "Creating symlink to README.md file"
rel_path=$(python3 -c "import os.path; print(os.path.relpath('${README_FILE}', '${DOCS_DIRECTORY}'))")
ln -sf "${rel_path}" "${DOC_FILE}"
echo "Created symlink: ${DOC_FILE} -> ${rel_path}"

for f in __init__.py client.py driver_test.py driver.py; do
    echo "Creating: ${MODULE_DIRECTORY}/${f}"
    envsubst < ${SCRIPT_DIR}/driver/jumpstarter_driver/${f}.tmpl > ${MODULE_DIRECTORY}/${f}
done

for f in .gitignore pyproject.toml examples/exporter.yaml; do
    echo "Creating: ${DRIVER_DIRECTORY}/${f}"
    envsubst < ${SCRIPT_DIR}/driver/${f}.tmpl > ${DRIVER_DIRECTORY}/${f}
done

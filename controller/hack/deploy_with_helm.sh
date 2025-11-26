#!/usr/bin/env bash
set -eo pipefail
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"

# Source common utilities
source "${SCRIPT_DIR}/utils"

# Source common deployment variables
source "${SCRIPT_DIR}/deploy_vars"

METHOD=install

kubectl config use-context kind-jumpstarter

# Install nginx ingress if in ingress mode
if [ "${NETWORKING_MODE}" == "ingress" ]; then
    install_nginx_ingress
else
    echo -e "${GREEN}Deploying with nodeport ...${NC}"
fi

# Build Helm sets based on configuration
HELM_SETS=""
HELM_SETS="${HELM_SETS} --set global.baseDomain=${BASEDOMAIN}"
HELM_SETS="${HELM_SETS} --set jumpstarter-controller.grpc.endpoint=${GRPC_ENDPOINT}"
HELM_SETS="${HELM_SETS} --set jumpstarter-controller.grpc.routerEndpoint=${GRPC_ROUTER_ENDPOINT}"
HELM_SETS="${HELM_SETS} --set jumpstarter-controller.image=${IMAGE_REPO}"
HELM_SETS="${HELM_SETS} --set jumpstarter-controller.tag=${IMAGE_TAG}"

# Enable appropriate networking mode
if [ "${NETWORKING_MODE}" == "ingress" ]; then
    HELM_SETS="${HELM_SETS} --set jumpstarter-controller.grpc.ingress.enabled=true"
else
    HELM_SETS="${HELM_SETS} --set jumpstarter-controller.grpc.nodeport.enabled=true"
fi

echo -e "${GREEN}Loading the ${IMG} in kind ...${NC}"
# load the docker image into the kind cluster
kind_load_image ${IMG}


# if we have an existing deployment, try to upgrade it instead
if helm list -A | grep jumpstarter > /dev/null; then
  METHOD=upgrade
fi

echo -e "${GREEN}Performing helm ${METHOD} ...${NC}"

# install/update with helm
# --skip-crds: CRDs are managed via templates/crds/ instead of the special crds/ directory
helm ${METHOD} --namespace jumpstarter-lab \
               --create-namespace \
               --skip-crds \
               ${HELM_SETS} \
               --set global.timestamp=$(date +%s) \
               --values ./deploy/helm/jumpstarter/values.kind.yaml ${EXTRA_VALUES} jumpstarter \
            ./deploy/helm/jumpstarter/

kubectl config set-context --current --namespace=jumpstarter-lab

# Check gRPC endpoints are ready
check_grpc_endpoints

# Print success banner
print_deployment_success "Helm"

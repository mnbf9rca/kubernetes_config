
# How to implement

well, you either implement with [rancher](https://github.com/mnbf9rca/kubernetes_config/blob/master/implement_rancher.md) (assuming you've set up rancher somewhere, e.g. DO), or with [microk8s](https://github.com/mnbf9rca/kubernetes_config/blob/master/implement_microk8s.md).

if implementing Longhorn on Microk8s remember that you need to provide the kubelet path because it's in a non-standard path, and i've mounted a separate drive at /mnt/longhorn

```shell
microk8s kubectl create namespace longhorn-system
# for testing use "/tmp/longhorn" as storage location
microk8s helm3 install longhorn longhorn/longhorn --namespace longhorn-system \
  --set defaultSettings.defaultDataPath="/mnt/longhorn" \
  --set csi.kubeletRootDir="/var/snap/microk8s/common/var/lib/kubelet"
```

in microk8s, enable:
- dashboard
- dns
- metallb
- ingress
- cert-manager


retrieve dashbaord token with `microk8s kubectl create token default`

install cluster issuer for cert-manager

install CSI driver for NFS: https://microk8s.io/docs/nfs

```
microk8s enable helm3
microk8s helm3 repo add csi-driver-nfs https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/master/charts
microk8s helm3 repo update
microk8s helm3 install csi-driver-nfs csi-driver-nfs/csi-driver-nfs \
    --namespace kube-system \
    --set kubeletDir=/var/snap/microk8s/common/var/lib/kubelet
microk8s kubectl wait pod --selector app.kubernetes.io/name=csi-driver-nfs --for condition=ready --namespace kube-system
```
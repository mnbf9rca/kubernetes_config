
# How to implement

well, you either implement with [rancher](https://github.com/mnbf9rca/kubernetes_config/blob/master/implement_rancher.md) (assuming you've set up rancher somewhere, e.g. DO), or with [microk8s](https://github.com/mnbf9rca/kubernetes_config/blob/master/implement_microk8s.md).

if implementing Longhorn on Microk8s remember that you need to provide the kubelet path because it's in a non-standard path

```shell
microk8s kubectl create namespace longhorn-system
# for testing use "/tmp/longhorn" as storage location
microk8s helm3 install longhorn longhorn/longhorn --namespace longhorn-system \
  --set defaultSettings.defaultDataPath="/longhorn" \
  --set csi.kubeletRootDir="/var/snap/microk8s/common/var/lib/kubelet"
```

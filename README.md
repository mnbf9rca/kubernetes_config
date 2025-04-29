
# How to implement

well, you either implement with [rancher](https://github.com/mnbf9rca/kubernetes_config/blob/master/implement_rancher.md) (assuming you've set up rancher somewhere, e.g. DO), or with microk8s.

# microk8s

## basics

inblock ufw: `sudo ufw allow from 127.0.0.1 port 19001`

in microk8s, enable:
- dashboard
- dns
- metallb:10.100.0.200-10.100.0.254
- ingress

store kubectl config:
```
cd $HOME
mkdir .kube
cd .kube
microk8s config > config
```

alias kubectl - add to `~/.bash_aliases` or `~/.bashrc`
```
alias kubectl='microk8s kubectl'
```

## cert manager
install cert-manger from helm or enable in microk8s
```
helm repo add jetstack https://charts.jetstack.io
helm repo update
helm install   cert-manager jetstack/cert-manager   --namespace cert-manager   --create-namespace  --set installCRDs=true
```

install cluster issuer for cert-manager

## longhorn

if implementing Longhorn on Microk8s remember that you need to provide the kubelet path because it's in a non-standard path, and i've mounted a separate drive at /mnt/longhorn

```shell
microk8s kubectl create namespace longhorn-system
# for testing use "/tmp/longhorn" as storage location
microk8s helm3 install longhorn longhorn/longhorn --namespace longhorn-system \
  --set defaultSettings.defaultDataPath="/mnt/longhorn" \
  --set csi.kubeletRootDir="/var/snap/microk8s/common/var/lib/kubelet"
```

read `longhorn-aws-secret.yml` to fetch the AWS backup secret and store it
install longhorn-ingress.yml
longhorn backup config:
- backup target: s3://<bucket>@<region>/ e.g. s3://longhorn-bucket@eu-west-2/
- backup target credential secret: `aws-secret`

## NFS

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

## dashboard

ignore default `microk8s kubectl describe secret -n kube-system microk8s-dashboard-token`
retrieve dashbaord token with `microk8s kubectl create token default  --duration 87600h`
install dashboard-ingress.yml

## downloads

create downloads namespace:
```
kubectl create namespace downloads
```

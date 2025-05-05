
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
- cert-manager

Apply metallb address pool: `kubectl apply -f addresspool.yaml`

install cluster issuer for cert-manager

store kubectl config:
```
cd $HOME
mkdir .kube
cd .kube
microk8s config > config
```

alias kubectl and helm - add to `~/.bash_aliases` or `~/.bashrc`
```
alias kubectl='microk8s kubectl'
alias helm='microk8s helm'
```


## longhorn

if implementing Longhorn on Microk8s remember that you need to provide the kubelet path because it's in a non-standard path, and i've mounted a separate drive at /mnt/longhorn

follow main instructions from https://longhorn.io/docs/1.8.1/deploy/install/install-with-helm/ but basically:


```shell
microk8s helm3 repo add longhorn https://charts.longhorn.io
microk8s helm3 repo update
microk8s kubectl create namespace longhorn-system
# for testing use "/tmp/longhorn" as storage location
microk8s helm3 install longhorn longhorn/longhorn --namespace longhorn-system \
  --set defaultSettings.defaultDataPath="/mnt/longhorn" \
  --set csi.kubeletRootDir="/var/snap/microk8s/common/var/lib/kubelet"
```

read `longhorn-aws-secret.yml` to create the AWS backup secret and store it - this includes the URL for the target
install longhorn-ingress.yml - remember to create the secret
`USER=<USERNAME_HERE>; PASSWORD=<PASSWORD_HERE>; echo "${USER}:$(openssl passwd -stdin -apr1 <<< ${PASSWORD})" >> auth`
`kubectl -n longhorn-system create secret generic basic-auth-longhorn --from-file=auth`
longhorn backup config:
- backup target: s3://<bucket>@<region>/ e.g. `s3://longhorn-bucket@eu-west-2/`
- backup target credential secret: `aws-secret` or `linode-secret`

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

Because of [CVE-2021-25742](https://github.com/kubernetes/kubernetes/issues/126811) you need to [enable annotation snippets](https://kubernetes.github.io/ingress-nginx/user-guide/nginx-configuration/configmap/#allow-snippet-annotations):
```
kubectl patch configmap nginx-load-balancer-microk8s-conf \
  -n ingress \
  --type merge \
  -p '{"data":{"allow-snippet-annotations":"true"}}'
```
then restart the deployment
`kubectl -n ingress rollout restart daemonset nginx-ingress-microk8s-controller`
check for pods with
`kubectl get pods -n ingress`


- Create admin user `htpasswd -c auth rob`
- Create secret `kubectl create secret generic basic-auth-dashboard --from-file=auth -n kube-system` 
  - (you might need `sudo apt-get install apache2-utils`)
- ignore default which is `microk8s kubectl describe secret -n kube-system microk8s-dashboard-token`
- retrieve dashbaord token with `microk8s kubectl create token default  --duration 87600h`
- install `dashboard-ingress.yml`

## downloads

create downloads namespace:
```
kubectl create namespace downloads
```

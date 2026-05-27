
some system configuration

```
sudo apt update
sudo apt install qemu-kvm libvirt-daemon-system libvirt-clients bridge-utils virt-manager \
                 libvirt-dev ruby-dev ruby-build build-essential -y
vagrant plugin install vagrant-libvirt
```

To start up vm
```
vagrant up --provider=libvirt
```


To connecto
```
vagrant ssh vm1
```

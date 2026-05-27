Vagrant.configure("2") do |config|
  
  # Use the official Ubuntu 22.04 LTS cloud image
	config.vm.box = "generic/ubuntu2204"

  # Define the First VM
  config.vm.define "vm1" do |vm1|
    vm1.vm.hostname = "research-node-1"
    vm1.vm.network "private_network", ip: "192.168.56.11", libvirt__forward_mode: "none"
    
    vm1.vm.provider :libvirt do |lv|
      lv.memory = 1024
      lv.cpus = 1
      # Explicitly pass the host's Intel CPU architecture/features to the guest
      lv.cpu_mode = "host-passthrough"
    end
  end

  # Define the Second VM
  config.vm.define "vm2" do |vm2|
    vm2.vm.hostname = "research-node-2"
    vm2.vm.network "private_network", ip: "192.168.56.12", libvirt__forward_mode: "none"
    
    vm2.vm.provider :libvirt do |lv|
      lv.memory = 1024
      lv.cpus = 1
      lv.cpu_mode = "host-passthrough"
    end
  end

end

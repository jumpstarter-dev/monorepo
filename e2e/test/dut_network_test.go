/*
Copyright 2026. The Jumpstarter Authors

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package e2e

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	. "github.com/onsi/ginkgo/v2" //nolint:revive
	. "github.com/onsi/gomega"    //nolint:revive
)

func hasPrivileges() bool {
	if os.Getuid() == 0 {
		return true
	}
	err := exec.Command("sudo", "-n", "true").Run() //nolint:gosec
	return err == nil
}

func needsSudo() bool {
	return os.Getuid() != 0
}

func sudoArgs(args ...string) (string, []string) {
	if needsSudo() {
		return "sudo", args
	}
	return args[0], args[1:]
}

// Serial: builds veth pairs, bridges and nftables rules in the host network
// namespace, and drives dnsmasq. There is only one host to share.
var _ = Describe("DUT Network E2E Tests", Label("dut-network"), Ordered, ContinueOnFailure, Serial, func() {
	var (
		tracker      *ProcessTracker
		listenerPort = 19091
		exporterDir  string
	)

	const (
		dutNs      = "jmp-e2e-dut"
		extNs      = "jmp-e2e-ext"
		vethHost   = "jmp-vhost"
		vethDut    = "jmp-vdut"
		vethUp     = "jmp-vup"
		vethExt    = "jmp-vext"
		nftTable   = "jumpstarter_jmp_vhost"
		dutIP      = "192.168.200.10"
		gatewayIP  = "192.168.200.1"
		extIP      = "10.99.0.1"
		upstreamIP = "10.99.0.2"
		subnet     = "192.168.200.0/24"
		vlanID     = 100
		vlanDutIP  = "192.168.200.50"
		vlanPubIP  = "10.100.0.50"
		vlanExtIP  = "10.100.0.1"
		pbrDutIP   = "192.168.200.51"
		noPbrVlan  = 101
		noPbrDutIP = "192.168.200.52"
		noPbrExtIP = "10.101.0.1"
	)

	setupNetworkNamespaces := func() {
		runOrFail("ip", "netns", "add", dutNs)
		runOrFail("ip", "netns", "add", extNs)

		runOrFail("ip", "link", "add", vethHost, "type", "veth", "peer", "name", vethDut)
		runOrFail("ip", "link", "set", vethDut, "netns", dutNs)
		runOrFail("ip", "link", "set", vethHost, "address", "02:00:00:00:00:01")

		runOrFail("ip", "link", "add", vethUp, "type", "veth", "peer", "name", vethExt)
		runOrFail("ip", "link", "set", vethExt, "netns", extNs)

		runOrFail("ip", "addr", "add", upstreamIP+"/24", "dev", vethUp)
		runOrFail("ip", "link", "set", vethUp, "up")

		runInNs(extNs, "ip", "addr", "add", extIP+"/24", "dev", vethExt)
		runInNs(extNs, "ip", "link", "set", vethExt, "up")
		runInNs(extNs, "ip", "link", "set", "lo", "up")
		runInNs(extNs, "ip", "route", "add", subnet, "via", upstreamIP)

		// Configure DUT ns with static IP
		runInNs(dutNs, "ip", "addr", "add", dutIP+"/24", "dev", vethDut)
		runInNs(dutNs, "ip", "link", "set", vethDut, "up")
		runInNs(dutNs, "ip", "link", "set", "lo", "up")
		runInNs(dutNs, "ip", "route", "add", "default", "via", gatewayIP)
	}

	teardownNetworkNamespaces := func() {
		runIgnoreErr("ip", "link", "del", vethHost)
		runIgnoreErr("ip", "link", "del", vethUp)
		runIgnoreErr("ip", "netns", "del", dutNs)
		runIgnoreErr("ip", "netns", "del", extNs)
		runIgnoreErr("nft", "delete", "table", "ip", nftTable)
		runIgnoreErr("rm", "-rf", "/tmp/jmp-e2e-dut-network")
	}

	BeforeAll(func() {
		if runtime.GOOS != "linux" {
			Skip("requires Linux")
		}
		if !hasPrivileges() {
			Skip("requires root or passwordless sudo")
		}
		tracker = NewProcessTracker()
		exporterDir = filepath.Join(RepoRoot(), "e2e", "exporters")
		teardownNetworkNamespaces()
		setupNetworkNamespaces()

		configPath := filepath.Join(exporterDir, "exporter-dut-network.yaml")
		tracker.StartDirectExporter(configPath, listenerPort, "", false)
		WaitForDirectExporterReady(listenerPort, "")
	})

	AfterAll(func() {
		tracker.StopAll()
		teardownNetworkNamespaces()

		Eventually(func() error {
			conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", listenerPort), 500*time.Millisecond)
			if err != nil {
				return nil
			}
			conn.Close()
			return fmt.Errorf("port %d is still open", listenerPort)
		}, 10*time.Second, 500*time.Millisecond).Should(Succeed(),
			"port %d should be closed after stopping exporter", listenerPort)

		tracker.Cleanup()
	})

	BeforeEach(func() {
		tracker.WriteLogMarker(CurrentSpecReport().FullText())
	})

	AfterEach(func() {
		if CurrentSpecReport().Failed() {
			tracker.DumpLogs(250)
		}
	})

	jmpShell := func(args ...string) (string, error) {
		shellArgs := []string{"shell", "--tls-grpc", fmt.Sprintf("127.0.0.1:%d", listenerPort),
			"--tls-grpc-insecure", "--"}
		shellArgs = append(shellArgs, args...)
		return Jmp(shellArgs...)
	}

	extractJSON := func(raw string) string {
		start := strings.Index(raw, "{")
		if start < 0 {
			return raw
		}
		return raw[start:]
	}

	addDutAddr := func(ip string) {
		runInNs(dutNs, "ip", "addr", "replace", ip+"/24", "dev", vethDut)
	}
	delDutAddr := func(ip string) {
		_, _ = runInNsCapture(dutNs, "ip", "addr", "del", ip+"/24", "dev", vethDut)
	}

	setupExtVLAN := func(id int, cidr string) string {
		return setupVLANInNs(extNs, vethExt, id, cidr)
	}

	Context("Network status", func() {
		It("should report network status via CLI", func() {
			out, err := jmpShell("j", "dut-network", "status")
			Expect(err).NotTo(HaveOccurred(), out)
			Expect(out).To(ContainSubstring(vethHost))
			Expect(out).To(ContainSubstring("masquerade"))

			var status map[string]interface{}
			err = json.Unmarshal([]byte(extractJSON(out)), &status)
			Expect(err).NotTo(HaveOccurred())
			Expect(status["interface_status"]).NotTo(BeNil())
		})
	})

	Context("DHCP leases", func() {
		It("should show leases via CLI", func() {
			out, err := jmpShell("j", "dut-network", "leases")
			Expect(err).NotTo(HaveOccurred(), out)
			Expect(out).NotTo(BeEmpty())
		})
	})

	Context("NAT rules", func() {
		It("should show active NAT rules", func() {
			out, err := jmpShell("j", "dut-network", "nat-rules")
			Expect(err).NotTo(HaveOccurred(), out)
			Expect(out).To(ContainSubstring("masquerade"))
			Expect(out).To(ContainSubstring(nftTable))
		})
	})

	Context("Connectivity", func() {
		It("should allow DUT to reach external via NAT", func() {
			expectPingNS(dutNs, "", extIP)
		})
	})

	Context("IP lookup", func() {
		It("should return error for unknown MAC", func() {
			out, err := jmpShell("j", "dut-network", "get-ip", "ff:ff:ff:ff:ff:ff")
			Expect(err).To(HaveOccurred())
			Expect(out).To(ContainSubstring("No lease found"))
		})
	})

	Context("Address management", func() {
		It("should add and remove an address entry via CLI", func() {
			out, err := jmpShell("j", "dut-network", "add-address",
				"192.168.200.99", "--mac", "02:00:00:00:00:99", "-n", "e2e-test")
			Expect(err).NotTo(HaveOccurred(), out)
			Expect(out).To(ContainSubstring("Added"))

			out, err = jmpShell("j", "dut-network", "remove-address", "192.168.200.99")
			Expect(err).NotTo(HaveOccurred(), out)
			Expect(out).To(ContainSubstring("Removed"))
		})
	})

	Context("DNS management", func() {
		It("should add, list, and remove DNS entries via CLI", func() {
			out, err := jmpShell("j", "dut-network", "add-dns",
				"e2e-test.lab.local", "10.0.0.42")
			Expect(err).NotTo(HaveOccurred(), out)
			Expect(out).To(ContainSubstring("Added"))

			out, err = jmpShell("j", "dut-network", "dns-entries")
			Expect(err).NotTo(HaveOccurred(), out)
			Expect(out).To(ContainSubstring("e2e-test.lab.local"))
			Expect(out).To(ContainSubstring("10.0.0.42"))

			out, err = jmpShell("j", "dut-network", "remove-dns", "e2e-test.lab.local")
			Expect(err).NotTo(HaveOccurred(), out)
			Expect(out).To(ContainSubstring("Removed"))

			out, err = jmpShell("j", "dut-network", "dns-entries")
			Expect(err).NotTo(HaveOccurred(), out)
			Expect(out).NotTo(ContainSubstring("e2e-test.lab.local"))
		})
	})

	Context("TCP connectivity", func() {
		It("should allow TCP connections from DUT to external via NAT", func() {
			expectTCPEcho(dutNs, extNs, "", extIP, 9998)
		})
	})

	Context("VLAN and policy-based routing", func() {
		It("should allow TCP from DUT via VLAN PBR", func() {
			extVlan := setupExtVLAN(vlanID, vlanExtIP+"/24")
			defer deleteLinkInNs(extNs, extVlan)

			out, err := jmpShell("j", "dut-network", "add-address",
				vlanDutIP, "--public-ip", vlanPubIP,
				"--vlan-id", fmt.Sprintf("%d", vlanID), "--public-gateway", vlanExtIP)
			Expect(err).NotTo(HaveOccurred(), out)

			addDutAddr(vlanDutIP)
			defer func() {
				delDutAddr(vlanDutIP)
				_, _ = jmpShell("j", "dut-network", "remove-address", vlanDutIP)
			}()

			expectTCPEcho(dutNs, extNs, vlanDutIP, vlanExtIP, 9998)
		})

		It("should allow TCP from DUT via untagged source-IP PBR", func() {
			out, err := jmpShell("j", "dut-network", "add-address",
				pbrDutIP, "--public-gateway", extIP)
			Expect(err).NotTo(HaveOccurred(), out)

			addDutAddr(pbrDutIP)
			defer func() {
				delDutAddr(pbrDutIP)
				_, _ = jmpShell("j", "dut-network", "remove-address", pbrDutIP)
			}()

			expectTCPEcho(dutNs, extNs, pbrDutIP, extIP, 9998)
		})

		It("should not reach a VLAN-only peer without public_gateway", func() {
			extVlan := setupExtVLAN(noPbrVlan, noPbrExtIP+"/24")
			defer deleteLinkInNs(extNs, extVlan)

			out, err := jmpShell("j", "dut-network", "add-address",
				noPbrDutIP, "--vlan-id", fmt.Sprintf("%d", noPbrVlan))
			Expect(err).NotTo(HaveOccurred(), out)

			addDutAddr(noPbrDutIP)
			defer func() {
				delDutAddr(noPbrDutIP)
				_, _ = jmpShell("j", "dut-network", "remove-address", noPbrDutIP)
			}()

			Expect(pingNS(dutNs, noPbrDutIP, noPbrExtIP)).To(HaveOccurred(),
				"DUT should not reach VLAN-only %s without public_gateway/PBR", noPbrExtIP)
			expectPingNS(dutNs, noPbrDutIP, extIP)
		})
	})
})

func runOrFail(args ...string) {
	bin, cmdArgs := sudoArgs(args...)
	cmd := exec.Command(bin, cmdArgs...) //nolint:gosec
	out, err := cmd.CombinedOutput()
	ExpectWithOffset(1, err).NotTo(HaveOccurred(),
		fmt.Sprintf("command %v failed: %s", args, string(out)))
}

func runIgnoreErr(args ...string) {
	bin, cmdArgs := sudoArgs(args...)
	cmd := exec.Command(bin, cmdArgs...) //nolint:gosec
	_ = cmd.Run()
}

func runInNs(ns string, args ...string) {
	fullArgs := append([]string{"ip", "netns", "exec", ns}, args...)
	bin, cmdArgs := sudoArgs(fullArgs...)
	cmd := exec.Command(bin, cmdArgs...) //nolint:gosec
	out, err := cmd.CombinedOutput()
	ExpectWithOffset(1, err).NotTo(HaveOccurred(),
		fmt.Sprintf("command in ns %s failed: %v -> %s", ns, args, string(out)))
}

func runInNsCapture(ns string, args ...string) (string, error) {
	fullArgs := append([]string{"ip", "netns", "exec", ns}, args...)
	bin, cmdArgs := sudoArgs(fullArgs...)
	cmd := exec.Command(bin, cmdArgs...) //nolint:gosec
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func deleteLinkInNs(ns, name string) {
	_, _ = runInNsCapture(ns, "ip", "link", "del", name)
}

func setupVLANInNs(ns, parent string, id int, cidr string) string {
	name := fmt.Sprintf("%s.%d", parent, id)
	deleteLinkInNs(ns, name)
	runInNs(ns, "ip", "link", "add", "link", parent, "name", name,
		"type", "vlan", "id", fmt.Sprintf("%d", id))
	runInNs(ns, "ip", "addr", "replace", cidr, "dev", name)
	runInNs(ns, "ip", "link", "set", name, "up")
	return name
}

func pingNS(ns, src, dst string) error {
	args := []string{"ping", "-c", "1", "-W", "2"}
	if src != "" {
		args = append(args, "-I", src)
	}
	args = append(args, dst)
	_, err := runInNsCapture(ns, args...)
	return err
}

func expectPingNS(ns, src, dst string) {
	GinkgoHelper()
	Eventually(func() error {
		return pingNS(ns, src, dst)
	}, 10*time.Second, 1*time.Second).Should(Succeed(),
		"namespace %s src %q should ping %s", ns, src, dst)
}

func tcpEchoServerScript(bind string, port int) string {
	return fmt.Sprintf(
		"import socket; "+
			"s=socket.socket(); "+
			"s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); "+
			"s.bind(('%s',%d)); "+
			"s.listen(1); "+
			"s.settimeout(10); "+
			"conn,_=s.accept(); "+
			"conn.sendall(b'E2E_OK'); "+
			"conn.close(); "+
			"s.close()",
		bind, port)
}

func tcpEchoClientScript(src, dst string, port int) string {
	if src == "" {
		return fmt.Sprintf(
			"import socket; "+
				"s=socket.create_connection(('%s',%d),timeout=5); "+
				"data=s.recv(10); "+
				"s.close(); "+
				"print(data.decode())",
			dst, port)
	}
	return fmt.Sprintf(
		"import socket; "+
			"s=socket.socket(); "+
			"s.settimeout(5); "+
			"s.bind(('%s',0)); "+
			"s.connect(('%s',%d)); "+
			"data=s.recv(10); "+
			"s.close(); "+
			"print(data.decode())",
		src, dst, port)
}

func startPythonInNs(ns, script string) (*exec.Cmd, error) {
	fullArgs := []string{"ip", "netns", "exec", ns, "python3", "-c", script}
	bin, cmdArgs := sudoArgs(fullArgs...)
	cmd := exec.Command(bin, cmdArgs...) //nolint:gosec
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func stopProcessGroup(cmd *exec.Cmd) {
	if cmd == nil || cmd.Process == nil {
		return
	}
	_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	_ = cmd.Wait()
}

func tcpEchoBetweenNS(dutNs, extNs, src, dst string, port int) (string, error) {
	bind := ""
	if src != "" {
		bind = dst
	}
	listener, err := startPythonInNs(extNs, tcpEchoServerScript(bind, port))
	if err != nil {
		return "", err
	}
	defer stopProcessGroup(listener)
	time.Sleep(500 * time.Millisecond)
	return runInNsCapture(dutNs, "python3", "-c", tcpEchoClientScript(src, dst, port))
}

func expectTCPEcho(dutNs, extNs, src, dst string, port int) {
	GinkgoHelper()
	out, err := tcpEchoBetweenNS(dutNs, extNs, src, dst, port)
	Expect(err).NotTo(HaveOccurred(),
		fmt.Sprintf("TCP %s -> %s:%d failed: %s", src, dst, port, out))
	Expect(out).To(ContainSubstring("E2E_OK"))
}

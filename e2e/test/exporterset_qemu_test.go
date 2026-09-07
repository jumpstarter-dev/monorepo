/*
Copyright 2026. The Jumpstarter Authors

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package e2e

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	. "github.com/onsi/ginkgo/v2" //nolint:revive
	. "github.com/onsi/gomega"    //nolint:revive
)

const exporterSetQemuClientName = "test-client-exporterset-qemu"

// Poll periods for the waits in this file. The conditions here are reached by a
// controller reacting to an event rather than by anything on a fixed schedule,
// so the poll period is almost entirely overshoot once the condition holds.
const (
	// qemuPollPeriod is for waits that run a single kubectl query per attempt.
	qemuPollPeriod = time.Second
	// qemuComposePollPeriod is for waits that run several queries per attempt,
	// where the attempt itself already costs a good fraction of a second.
	qemuComposePollPeriod = 2 * time.Second
)

// qemuGuestArch holds native ExporterSet QEMU e2e identifiers for the host.
type qemuGuestArch struct {
	Arch       string
	Board      string
	Selector   string
	QemuBinary string
	Manifest   string
}

func loadQemuGuestArch() qemuGuestArch {
	GinkgoHelper()
	out := MustRunCmd("bash", filepath.Join(RepoRoot(), "e2e", "scripts", "qemu-guest-arch.sh"))
	vals := map[string]string{}
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		vals[k] = v
	}
	Expect(vals["GUEST_ARCH"]).NotTo(BeEmpty(), "qemu-guest-arch.sh missing GUEST_ARCH")
	Expect(vals["BOARD"]).NotTo(BeEmpty(), "qemu-guest-arch.sh missing BOARD")
	Expect(vals["QEMU_BINARY"]).NotTo(BeEmpty(), "qemu-guest-arch.sh missing QEMU_BINARY")
	Expect(vals["MANIFEST"]).NotTo(BeEmpty(), "qemu-guest-arch.sh missing MANIFEST")
	return qemuGuestArch{
		Arch:       vals["GUEST_ARCH"],
		Board:      vals["BOARD"],
		Selector:   "board=" + vals["BOARD"],
		QemuBinary: vals["QEMU_BINARY"],
		Manifest:   vals["MANIFEST"],
	}
}

// Serial: boots VMs under TCG emulation, which will starve every other spec
// on the runner if it shares the CPU.
var _ = Describe("ExporterSet QEMU E2E Tests", Label("exporterset-qemu"), Ordered, Serial, func() {
	var (
		ns        string
		manifest  string
		imagePath string
		script    string
		guest     qemuGuestArch
	)

	BeforeAll(func() {
		ns = Namespace()
		guest = loadQemuGuestArch()
		manifest = filepath.Join(RepoRoot(), guest.Manifest)
		script = filepath.Join(RepoRoot(), "e2e", "scripts", "qemu_flash_boot.py")

		By(fmt.Sprintf("using native QEMU guest arch %s (%s, %s)", guest.Arch, guest.QemuBinary, guest.Manifest))
		Expect(manifest).To(BeAnExistingFile())

		By("ensuring Alpine guest image is available")
		out := MustRunCmd("bash", filepath.Join(RepoRoot(), "e2e", "scripts", "ensure-qemu-guest-image.sh"))
		lines := strings.Split(strings.TrimSpace(out), "\n")
		imagePath = lines[len(lines)-1]
		Expect(imagePath).NotTo(BeEmpty())
		Expect(imagePath).To(BeAnExistingFile())
		Expect(imagePath).To(ContainSubstring(guest.Arch),
			"guest image path should match detected arch %s", guest.Arch)

		By("waiting for exporterset-controller Deployment")
		WaitForDeploymentAvailable("component=exporterset-controller", 5*time.Minute)

		By("creating and logging in e2e client")
		EnsureOIDCClient(exporterSetQemuClientName)

		By("applying ExporterSet QEMU kind manifest")
		MustKubectl("apply", "-f", manifest)
	})

	AfterAll(func() {
		By("cleaning up ExporterSet resources and client")
		if manifest != "" {
			_, _ = Kubectl("delete", "--ignore-not-found", "-f", manifest)
		}
		DeleteClient(exporterSetQemuClientName)
	})

	AfterEach(func() {
		DumpOnFailure(250, func(maxLines int) {
			DumpExporterSetQemuLogs(maxLines, guest.Selector)
		})
	})

	It("brings an Exporter Online with a Ready Pod", func() {
		By("waiting for ExporterSet to create an exporter")
		var exporterName string
		Eventually(func() string {
			exporterName = KubectlQuery("-n", ns, "get", "exporter",
				"-l", guest.Selector,
				"-o", "jsonpath={.items[0].metadata.name}")
			return exporterName
		}, 5*time.Minute, qemuPollPeriod).ShouldNot(BeEmpty())

		By(fmt.Sprintf("waiting for exporter %s Online/Registered/Available", exporterName))
		WaitForExporter(exporterName)

		By("waiting for Pod Ready")
		Eventually(func() string {
			return KubectlQuery("-n", ns, "get", "pod", exporterName,
				"-o", "jsonpath={.status.phase}")
		}, 5*time.Minute, qemuPollPeriod).Should(Equal("Running"))

		Eventually(func() string {
			return KubectlQuery("-n", ns, "get", "pod", exporterName,
				"-o", "jsonpath={.status.containerStatuses[*].ready}")
		}, 5*time.Minute, qemuPollPeriod).Should(ContainSubstring("true"))

		By(fmt.Sprintf("verifying runtime image provides %s", guest.QemuBinary))
		// fedora-minimal has no `which`; use a shell builtin.
		binPath, err := Kubectl("-n", ns, "exec", exporterName, "-c", "target-runtime",
			"--", "sh", "-c", "command -v "+guest.QemuBinary)
		Expect(err).NotTo(HaveOccurred(), "command -v %s: %s", guest.QemuBinary, binPath)
		Expect(strings.TrimSpace(binPath)).To(ContainSubstring(guest.QemuBinary))
	})

	It("leases, flashes Alpine, and boots to a console login marker", func() {
		By("running flash+boot helper under jmp shell")
		// Long timeout: Kind uses TCG emulation without KVM.
		cmd := JmpCmd(
			"shell",
			"--client", exporterSetQemuClientName,
			"--selector", guest.Selector,
			"--duration", "1h",
			"--",
			"python3", script,
			"--timeout", "900",
			"--disk-size", "2G",
			imagePath,
		)
		cmd.Env = append(os.Environ(), "JUMPSTARTER_GRPC_INSECURE=1")
		out, err := cmd.CombinedOutput()
		GinkgoWriter.Write(out)
		Expect(err).NotTo(HaveOccurred(), "qemu_flash_boot.py failed: %s", string(out))
		Expect(string(out)).To(ContainSubstring("OK: matched marker"))
	})

	It("power cycles QEMU then rotates the Pod/Exporter and stays responsive", func() {
		By("recording the current Running Pod name and UID")
		var oldName, oldUID string
		Eventually(func(g Gomega) {
			out, err := Kubectl("-n", ns, "get", "pod",
				"-l", guest.Selector,
				"--field-selector=status.phase=Running",
				"-o", "jsonpath={.items[0].metadata.name}")
			g.Expect(err).NotTo(HaveOccurred())
			g.Expect(out).NotTo(BeEmpty())
			oldName = out
			uid, err := Kubectl("-n", ns, "get", "pod", oldName,
				"-o", "jsonpath={.metadata.uid}")
			g.Expect(err).NotTo(HaveOccurred())
			g.Expect(uid).NotTo(BeEmpty())
			oldUID = uid
		}, 2*time.Minute, qemuComposePollPeriod).Should(Succeed())

		By(fmt.Sprintf("power on, assert %s is running, then power off", guest.QemuBinary))
		// One lease: start QEMU via the runtime sidecar, confirm the expected
		// qemu-system-* binary is the process that started, stop it, then
		// release so exitOnLeaseEnd completes the Pod and ExitAndReplace recycles it.
		powerScript := fmt.Sprintf(`
set -eu
j qemu power on
pod=$(kubectl -n %q get pod -l %q --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
ps_out=$(kubectl -n %q exec "$pod" -c target-runtime -- ps -eo args=)
echo "$ps_out"
echo "$ps_out" | grep -F %q
j qemu power off
`, ns, guest.Selector, ns, guest.QemuBinary)
		MustJmp("shell", "--client", exporterSetQemuClientName,
			"--selector", guest.Selector,
			"--duration", "5m",
			"--", "sh", "-c", powerScript)

		By("waiting for the old Pod/Exporter to be deleted and a single replacement Running")
		Eventually(func(g Gomega) {
			// Old Completed instance must be gone (controller deletes Exporter → cascade Pod).
			_, err := Kubectl("-n", ns, "get", "pod", oldName)
			g.Expect(err).To(HaveOccurred(), "old Pod %s should be deleted after ExitAndReplace", oldName)

			_, err = Kubectl("-n", ns, "get", "exporter", oldName)
			g.Expect(err).To(HaveOccurred(), "old Exporter %s should be deleted after ExitAndReplace", oldName)

			podNames, err := Kubectl("-n", ns, "get", "pod",
				"-l", guest.Selector,
				"--field-selector=status.phase=Running",
				"-o", "jsonpath={range .items[*]}{.metadata.name}{' '}{end}")
			g.Expect(err).NotTo(HaveOccurred())
			names := strings.Fields(strings.TrimSpace(podNames))
			g.Expect(names).To(HaveLen(1), "expected exactly one Running Pod, got %v", names)

			uid, err := Kubectl("-n", ns, "get", "pod", names[0],
				"-o", "jsonpath={.metadata.uid}")
			g.Expect(err).NotTo(HaveOccurred())
			g.Expect(uid).NotTo(Equal(oldUID), "replacement Pod should have a new UID")

			ready, err := Kubectl("-n", ns, "get", "pod", names[0],
				"-o", "jsonpath={.status.containerStatuses[*].ready}")
			g.Expect(err).NotTo(HaveOccurred())
			g.Expect(ready).To(ContainSubstring("true"))

			exporters, err := Kubectl("-n", ns, "get", "exporter",
				"-l", guest.Selector,
				"-o", "jsonpath={range .items[*]}{.metadata.name}{' '}{end}")
			g.Expect(err).NotTo(HaveOccurred())
			g.Expect(strings.Fields(strings.TrimSpace(exporters))).To(HaveLen(1),
				"expected exactly one Exporter after recycle, got %q", exporters)
		}, 5*time.Minute, qemuComposePollPeriod).Should(Succeed())

		By("waiting for the replacement exporter to become Available")
		var exporterName string
		Eventually(func() string {
			exporterName = KubectlQuery("-n", ns, "get", "exporter",
				"-l", guest.Selector,
				"-o", "jsonpath={.items[0].metadata.name}")
			return exporterName
		}, 2*time.Minute, qemuPollPeriod).ShouldNot(BeEmpty())
		WaitForExporter(exporterName)

		By("verifying the replacement still responds to qemu power on/off")
		MustJmp("shell", "--client", exporterSetQemuClientName,
			"--selector", guest.Selector,
			"--duration", "5m",
			"--", "sh", "-c", "j qemu power on && j qemu power off")
	})
})

// DumpExporterSetQemuLogs prints recent logs from exporterset-controller and
// virtual QEMU pods for failure diagnosis.
func DumpExporterSetQemuLogs(maxLines int, selector string) {
	ns := Namespace()
	if selector == "" {
		selector = "virtual=true"
	}
	_, _ = fmt.Fprintf(GinkgoWriter, "=== ExporterSet / QEMU pod logs (last %d lines, %s) ===\n", maxLines, selector)

	out, _ := Kubectl("-n", ns, "get", "pods",
		"-l", selector,
		"-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}")
	for _, name := range strings.Split(strings.TrimSpace(out), "\n") {
		if name == "" {
			continue
		}
		_, _ = fmt.Fprintf(GinkgoWriter, "--- pod/%s exporter (main) ---\n", name)
		logs, _ := Kubectl("-n", ns, "logs", name, "-c", "exporter",
			"--tail", fmt.Sprintf("%d", maxLines))
		_, _ = fmt.Fprintln(GinkgoWriter, logs)
		_, _ = fmt.Fprintf(GinkgoWriter, "--- pod/%s target-runtime (sidecar) ---\n", name)
		logs, _ = Kubectl("-n", ns, "logs", name, "-c", "target-runtime",
			"--tail", fmt.Sprintf("%d", maxLines))
		_, _ = fmt.Fprintln(GinkgoWriter, logs)
	}

	out, _ = Kubectl("-n", ns, "get", "deploy",
		"-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}")
	for _, name := range strings.Split(strings.TrimSpace(out), "\n") {
		if !strings.Contains(name, "exporterset") {
			continue
		}
		_, _ = fmt.Fprintf(GinkgoWriter, "--- deploy/%s ---\n", name)
		logs, _ := Kubectl("-n", ns, "logs", "deploy/"+name, "--tail", fmt.Sprintf("%d", maxLines))
		_, _ = fmt.Fprintln(GinkgoWriter, logs)
	}
}

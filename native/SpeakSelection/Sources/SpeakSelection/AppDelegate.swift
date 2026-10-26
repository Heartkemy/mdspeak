import AppKit
import ServiceManagement

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private var hotKey: HotKey?
    private var loginItem: NSMenuItem!
    private var axGrantedItem: NSMenuItem!
    private var axMissingItem: NSMenuItem!
    private var playerWindow: PlayerWindow?

    private let worker = Worker()
    private let player = Player()
    private var animateTimer: Timer?
    private var frame = 0
    private var state: IndicatorState = .idle

    /// Monotonic token for the most recent speak request. Stop() (or a failed
    /// load) bumps it so a worker reply that lands later can't start audio the
    /// user already cancelled.
    private var speakToken = 0
    private var activeJobId: String?
    /// Safety net: clears the hourglass if no audio has started within a bound.
    private var generatingWatchdog: Timer?

    private enum IndicatorState {
        case idle
        case generating
        case speaking
    }

    private let idleIcon = "speaker.wave.2.fill"

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupMenu()
        registerHotKey()
        bindPlayer()
        worker.start()
        checkAccessibility()
    }

    // ---------------------------------------------------------------- menu

    private func setupMenu() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: idleIcon, accessibilityDescription: "Speak Selection")
        }

        let menu = NSMenu()
        menu.delegate = self

        let speakItem = NSMenuItem(title: "Speak Selection", action: #selector(toggleSpeak), keyEquivalent: "")
        speakItem.target = self
        menu.addItem(speakItem)

        let stopItem = NSMenuItem(title: "Stop", action: #selector(stop), keyEquivalent: "")
        stopItem.target = self
        menu.addItem(stopItem)

        menu.addItem(.separator())

        let playerItem = NSMenuItem(title: "Player…", action: #selector(openPlayer), keyEquivalent: ",")
        playerItem.target = self
        menu.addItem(playerItem)

        menu.addItem(.separator())

        // Accessibility status — refreshed each time the menu opens.
        axGrantedItem = NSMenuItem(title: "✓ Accessibility granted", action: nil, keyEquivalent: "")
        axGrantedItem.isEnabled = false
        menu.addItem(axGrantedItem)

        axMissingItem = NSMenuItem(title: "Grant Accessibility Permission…", action: #selector(grantAccessibility), keyEquivalent: "")
        axMissingItem.target = self
        menu.addItem(axMissingItem)

        menu.addItem(.separator())

        loginItem = NSMenuItem(title: "Launch at Login", action: #selector(toggleLoginItem), keyEquivalent: "")
        loginItem.target = self
        loginItem.state = loginState()
        menu.addItem(loginItem)

        menu.addItem(.separator())

        let diagItem = NSMenuItem(title: "Copy Selection Diagnostics", action: #selector(copyDiagnostics), keyEquivalent: "")
        diagItem.target = self
        menu.addItem(diagItem)

        let quitItem = NSMenuItem(title: "Quit Speak Selection", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        statusItem.menu = menu
    }

    func menuWillOpen(_ menu: NSMenu) {
        let trusted = AXIsProcessTrusted()
        axGrantedItem.isHidden = !trusted
        axMissingItem.isHidden = trusted
        loginItem.state = loginState()
    }

    private func registerHotKey() {
        // ⌃⌥S — Carbon controlKey (0x1000) | optionKey (0x0800), key code 1 = ANSI 'S'.
        hotKey = HotKey(keyCode: 1, modifiers: 0x1800) { [weak self] in
            self?.toggleSpeak()
        }
    }

    private func bindPlayer() {
        player.onPlayStateChange = { [weak self] playing in
            DispatchQueue.main.async { self?.handlePlayState(playing) }
        }
        player.onLoadFailure = { [weak self] in
            DispatchQueue.main.async { self?.handlePlaybackFailure() }
        }
    }

    private func handlePlayState(_ playing: Bool) {
        if playing {
            stopGeneratingWatchdog()
            setState(.speaking)
        } else if state == .speaking {
            // Finished, stopped, or paused — no audio is coming out.
            setState(.idle)
        }
        // A false tick while .generating is the run resetting before its first
        // segment is audible; stay on the hourglass until audio actually starts.
    }

    private func handlePlaybackFailure() {
        stop()
        NSSound.beep()
    }

    // ---------------------------------------------------------------- speak

    @objc private func toggleSpeak() {
        if state == .generating {
            stop() // the model is still rendering — let the hotkey cut it off
            return
        }
        if player.isPlaying {
            stop()
            return
        }
        speak()
    }

    private func speak() {
        let text = SelectionReader.selectedText() ?? SelectionReader.clipboardText()
        guard let text, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        stop()
        let voice = AppSettings.voice
        let calm = AppSettings.calm
        speakToken &+= 1
        let token = speakToken
        setState(.generating)
        startGeneratingWatchdog()
        worker.speak(text: text, engine: AppSettings.engine, voice: voice, calm: calm) { [weak self] job in
            DispatchQueue.main.async {
                guard let self else { return }
                guard token == self.speakToken else {
                    if let job { self.worker.cancel(jobId: job.id) }
                    return
                }
                guard let job else {
                    self.stopGeneratingWatchdog()
                    self.setState(.idle)
                    NSSound.beep()
                    return
                }
                // Stay on the hourglass (watchdog still armed) until the first
                // segment is actually audible.
                let id = job.id
                self.activeJobId = id
                let count = job.segments
                self.player.playSegments(total: count, rate: Float(AppSettings.speed)) { [weak self] index in
                    self?.worker.fetchSegment(jobId: id, index: index)
                }
            }
        }
    }

    @objc private func stop() {
        if let id = activeJobId { worker.cancel(jobId: id) }
        activeJobId = nil
        speakToken &+= 1
        stopGeneratingWatchdog()
        player.stop()
        setState(.idle)
    }

    // ---------------------------------------------------------------- generating watchdog

    /// The hourglass must not hang forever: if no audio has started this long
    /// after a speak, the worker is wedged — bail out and beep.
    private func startGeneratingWatchdog() {
        stopGeneratingWatchdog()
        // Allow the worker's bounded cloud request to finish or report its error.
        generatingWatchdog = Timer.scheduledTimer(withTimeInterval: 150, repeats: false) { [weak self] _ in
            guard let self else { return }
            if self.state == .generating {
                self.stop()
                NSSound.beep()
            }
        }
    }

    private func stopGeneratingWatchdog() {
        generatingWatchdog?.invalidate()
        generatingWatchdog = nil
    }

    // ---------------------------------------------------------------- indicator

    private func setState(_ newState: IndicatorState) {
        state = newState
        switch newState {
        case .idle:
            animateTimer?.invalidate()
            animateTimer = nil
            setIcon(idleIcon)
        case .generating:
            startTicking(0.5, ["hourglass", "hourglass.bottomhalf.filled"])
        case .speaking:
            startTicking(0.4, ["speaker.wave.2.fill", "speaker.wave.3.fill"])
        }
    }

    private func startTicking(_ interval: TimeInterval, _ icons: [String]) {
        animateTimer?.invalidate()
        frame = 0
        setIcon(icons[0])
        animateTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.frame = (self.frame + 1) % icons.count
            self.setIcon(icons[self.frame])
        }
    }

    private func setIcon(_ name: String) {
        statusItem.button?.image = NSImage(
            systemSymbolName: name, accessibilityDescription: "Speak Selection"
        )
    }

    // ---------------------------------------------------------------- player window

    @objc private func openPlayer() {
        if playerWindow == nil {
            playerWindow = PlayerWindow(worker: worker, player: player)
        }
        playerWindow?.showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // ---------------------------------------------------------------- login

    private func loginState() -> NSControl.StateValue {
        if #available(macOS 13.0, *) {
            return SMAppService.mainApp.status == .enabled ? .on : .off
        }
        return .off
    }

    @objc private func toggleLoginItem() {
        guard #available(macOS 13.0, *) else { return }
        let service = SMAppService.mainApp
        do {
            if loginItem.state == .on {
                try service.unregister()
                loginItem.state = .off
            } else {
                try service.register()
                loginItem.state = .on
            }
        } catch {
            // Only works when bundled + signed; silently no-op otherwise.
        }
    }

    @objc private func quit() {
        stop()
        worker.stop()
        NSApp.terminate(nil)
    }

    // ---------------------------------------------------------------- accessibility

    private func checkAccessibility() {
        // Prompt until granted, then stop. With the stable signing identity the
        // grant persists across rebuilds, so this only reappears if it's missing.
        if !AXIsProcessTrusted() {
            grantAccessibility()
        }
    }

    @objc private func grantAccessibility() {
        let _ = AXIsProcessTrustedWithOptions(
            [kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary
        )
    }

    @objc private func copyDiagnostics() {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(SelectionReader.diagnostics(), forType: .string)
    }
}

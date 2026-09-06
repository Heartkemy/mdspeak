import AppKit

/// A slider that reports whether the mouse is dragging it, so the progress
/// timer doesn't fight the user mid-scrub.
private final class TrackingSlider: NSSlider {
    private(set) var tracking = false
    override func mouseDown(with event: NSEvent) {
        tracking = true
        super.mouseDown(with: event)
        tracking = false
    }
}

/// The player/settings window: a scrubber with transport controls, plus the
/// voice / speed / calm settings. Built programmatically — no XIB.
final class PlayerWindow: NSWindowController {
    private let worker: Worker
    private let player: Player

    private var scrubber: TrackingSlider!
    private var currentLabel: NSTextField!
    private var durationLabel: NSTextField!
    private var playPauseButton: NSButton!
    private var voicePopup: NSPopUpButton!
    private var speedSlider: NSSlider!
    private var speedLabel: NSTextField!
    private var calmCheckbox: NSButton!

    private static let sideInset: CGFloat = 26
    private static let labelWidth: CGFloat = 48

    init(worker: Worker, player: Player) {
        self.worker = worker
        self.player = player
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 500, height: 300),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Speak Selection"
        window.isReleasedWhenClosed = false
        window.center()
        super.init(window: window)
        buildContent()
        bindPlayer()
        refreshVoices()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    // ---------------------------------------------------------------- layout

    private func buildContent() {
        guard let content = window?.contentView else { return }

        // Vertical stack. The scrub row and the separator span the full width;
        // the transport stays centered; the settings read as a left-aligned
        // column so controls line up under a shared left edge.
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 18
        stack.translatesAutoresizingMaskIntoConstraints = false

        // Scrubber + time labels. Labels keep fixed monospaced widths so they
        // don't jitter; the slider absorbs the rest of the row.
        currentLabel = timeLabel()
        durationLabel = timeLabel()
        scrubber = TrackingSlider(value: 0, minValue: 0, maxValue: 1, target: self, action: #selector(scrub))
        scrubber.isContinuous = true
        scrubber.translatesAutoresizingMaskIntoConstraints = false
        scrubber.setContentHuggingPriority(.defaultLow, for: .horizontal)
        currentLabel.widthAnchor.constraint(equalToConstant: 44).isActive = true
        durationLabel.widthAnchor.constraint(equalToConstant: 44).isActive = true
        scrubber.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        let scrubRow = row(currentLabel, scrubber, durationLabel)
        scrubRow.distribution = .fill

        // Transport: ±15 s — play/pause (prominent accent) — +15 s, centered.
        let back = transportButton("gobackward.15", #selector(skipBack))
        playPauseButton = transportButton("play.fill", #selector(togglePlayPause), prominent: true)
        let forward = transportButton("goforward.15", #selector(skipForward))
        let transport = NSStackView(views: [back, playPauseButton, forward])
        transport.orientation = .horizontal
        transport.spacing = 26
        transport.alignment = .centerY
        let transportWrap = NSStackView(views: [transport])
        transportWrap.orientation = .vertical
        transportWrap.alignment = .centerX
        transportWrap.translatesAutoresizingMaskIntoConstraints = false

        // Settings
        let separator = NSBox()
        separator.boxType = .separator

        let voiceLabel = settingTitle("Voice")
        voicePopup = NSPopUpButton(frame: .zero, pullsDown: false)
        voicePopup.target = self
        voicePopup.action = #selector(voiceChanged)
        voicePopup.setContentHuggingPriority(.defaultLow, for: .horizontal)
        voicePopup.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        let voiceRow = row(voiceLabel, voicePopup)

        let speedTitle = settingTitle("Speed")
        speedSlider = NSSlider(value: AppSettings.speed, minValue: 0.6, maxValue: 2.0, target: self, action: #selector(speedChanged))
        speedSlider.setContentHuggingPriority(.defaultLow, for: .horizontal)
        speedSlider.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        speedLabel = NSTextField(labelWithString: formatSpeed(AppSettings.speed))
        speedLabel.alignment = .right
        speedLabel.font = .monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        speedLabel.translatesAutoresizingMaskIntoConstraints = false
        speedLabel.widthAnchor.constraint(equalToConstant: 52).isActive = true
        let speedRow = row(speedTitle, speedSlider, speedLabel)

        calmCheckbox = NSButton(checkboxWithTitle: "Calm — slower pace, longer pauses", target: self, action: #selector(calmChanged))
        calmCheckbox.state = AppSettings.calm ? .on : .off

        stack.addArrangedSubview(scrubRow)
        stack.addArrangedSubview(transportWrap)
        stack.addArrangedSubview(separator)
        stack.addArrangedSubview(voiceRow)
        stack.addArrangedSubview(speedRow)
        stack.addArrangedSubview(calmCheckbox)

        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(greaterThanOrEqualTo: content.topAnchor, constant: 18),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor, constant: -18),
            stack.centerYAnchor.constraint(equalTo: content.centerYAnchor),
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: Self.sideInset),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -Self.sideInset),
            // Full-width rows.
            scrubRow.widthAnchor.constraint(equalTo: stack.widthAnchor),
            transportWrap.widthAnchor.constraint(equalTo: stack.widthAnchor),
            separator.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])
    }

    private func row(_ views: NSView...) -> NSStackView {
        let row = NSStackView(views: views)
        row.orientation = .horizontal
        row.spacing = 10
        row.alignment = .centerY
        return row
    }

    private func settingTitle(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.translatesAutoresizingMaskIntoConstraints = false
        label.widthAnchor.constraint(equalToConstant: Self.labelWidth).isActive = true
        return label
    }

    private func timeLabel() -> NSTextField {
        let label = NSTextField(labelWithString: "0:00")
        label.font = .monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        label.alignment = .center
        return label
    }

    private func transportButton(_ name: String, _ action: Selector, prominent: Bool = false) -> NSButton {
        var image = NSImage(systemSymbolName: name, accessibilityDescription: nil)!
        let config = NSImage.SymbolConfiguration(pointSize: prominent ? 20 : 16, weight: .semibold)
        image = image.withSymbolConfiguration(config)!
        image.isTemplate = true
        let btn = NSButton(image: image, target: self, action: action)
        btn.imageScaling = .scaleProportionallyDown
        btn.translatesAutoresizingMaskIntoConstraints = false
        btn.widthAnchor.constraint(equalToConstant: prominent ? 40 : 32).isActive = true
        btn.heightAnchor.constraint(equalToConstant: prominent ? 40 : 32).isActive = true
        if prominent {
            btn.isBordered = true
            btn.bezelStyle = .regularSquare
            btn.bezelColor = .controlAccentColor
            btn.contentTintColor = .white
        } else {
            btn.isBordered = false
            btn.contentTintColor = .secondaryLabelColor
        }
        return btn
    }

    // ---------------------------------------------------------------- player binding

    private func bindPlayer() {
        player.onTick = { [weak self] current, duration in
            DispatchQueue.main.async { self?.updateScrubber(current, duration) }
        }
        player.onPlayStateChange = { [weak self] playing in
            DispatchQueue.main.async { self?.updatePlayPause(playing) }
        }
    }

    private func updateScrubber(_ current: Double, _ duration: Double) {
        if !scrubber.tracking {
            scrubber.maxValue = max(duration, 0.1)
            scrubber.doubleValue = current
        }
        currentLabel.stringValue = formatTime(current)
        durationLabel.stringValue = formatTime(duration)
    }

    private func updatePlayPause(_ playing: Bool) {
        let name = playing ? "pause.fill" : "play.fill"
        var image = NSImage(systemSymbolName: name, accessibilityDescription: nil)!
        image = image.withSymbolConfiguration(.init(pointSize: 20, weight: .semibold))!
        image.isTemplate = true
        playPauseButton.image = image
    }

    // ---------------------------------------------------------------- transport actions

    @objc private func scrub() {
        player.seek(to: scrubber.doubleValue)
    }

    @objc private func togglePlayPause() {
        player.togglePlayPause()
    }

    @objc private func skipBack() {
        player.skip(by: -15)
    }

    @objc private func skipForward() {
        player.skip(by: 15)
    }

    // ---------------------------------------------------------------- settings actions

    private func refreshVoices() {
        let current = AppSettings.voice
        voicePopup.removeAllItems()
        voicePopup.addItem(withTitle: "Loading…")
        worker.voices { [weak self] voices in
            DispatchQueue.main.async {
                guard let self else { return }
                self.voicePopup.removeAllItems()
                if voices.isEmpty {
                    self.voicePopup.addItem(withTitle: current)
                } else {
                    for v in voices {
                        self.voicePopup.addItem(withTitle: v.label)
                        self.voicePopup.lastItem?.representedObject = v.id
                    }
                    self.selectVoice(current)
                }
            }
        }
    }

    private func selectVoice(_ id: String) {
        for item in voicePopup.itemArray {
            if let obj = item.representedObject as? String, obj == id {
                voicePopup.select(item)
                return
            }
        }
    }

    @objc private func voiceChanged() {
        if let id = voicePopup.selectedItem?.representedObject as? String {
            AppSettings.voice = id
        }
    }

    @objc private func speedChanged() {
        let v = speedSlider.doubleValue
        AppSettings.speed = v
        speedLabel.stringValue = formatSpeed(v)
        player.setRate(Float(v))
    }

    @objc private func calmChanged() {
        AppSettings.calm = calmCheckbox.state == .on
    }

    private func formatSpeed(_ v: Double) -> String {
        String(format: "%.2f×", v)
    }

    private func formatTime(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds >= 0 else { return "0:00" }
        let s = Int(seconds)
        return String(format: "%d:%02d", s / 60, s % 60)
    }
}

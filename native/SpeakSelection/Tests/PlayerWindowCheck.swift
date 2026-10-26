import AppKit

@main
struct PlayerWindowCheck {
    static func main() {
        _ = NSApplication.shared
        let defaults = UserDefaults.standard
        let keys = ["engine", "voice", "cloudflareVoice", "geminiVoice"]
        let saved = keys.map { defaults.object(forKey: $0) }
        defer {
            for (key, value) in zip(keys, saved) {
                if let value { defaults.set(value, forKey: key) }
                else { defaults.removeObject(forKey: key) }
            }
        }
        defaults.removeObject(forKey: "engine")
        assert(AppSettings.engine == .kokoro, "New installs must default to local speech")
        defaults.set("kokoro", forKey: "engine")
        defaults.set("bm_george", forKey: "voice") // legacy local preference
        defaults.set("orion", forKey: "cloudflareVoice")
        defaults.set("Kore", forKey: "geminiVoice")
        let player = Player()
        var states: [Bool] = []
        // AppDelegate uses this observer to cancel the startup watchdog.
        player.onPlayStateChange = { states.append($0) }

        let window = PlayerWindow(worker: Worker(port: 0), player: player)
        func popups(in view: NSView) -> [NSPopUpButton] {
            (view as? NSPopUpButton).map { [$0] } ?? view.subviews.flatMap { popups(in: $0) }
        }
        let choices = popups(in: window.window!.contentView!)
            .flatMap { $0.itemArray.compactMap { $0.representedObject as? String } }
        guard Set(["kokoro", "cloudflare", "gemini"]).isSubset(of: Set(choices)) else {
            fputs("FAIL: native settings must offer local Kokoro alongside hosted providers\n", stderr)
            exit(1)
        }
        let provider = popups(in: window.window!.contentView!).first {
            $0.itemArray.contains { ($0.representedObject as? String) == "kokoro" }
        }!
        assert(provider.selectedItem?.representedObject as? String == "kokoro")
        for (id, voice) in [("cloudflare", "orion"), ("gemini", "Kore"), ("kokoro", "bm_george")] {
            provider.select(provider.itemArray.first { ($0.representedObject as? String) == id })
            assert(NSApp.sendAction(provider.action!, to: provider.target, from: provider))
            assert(defaults.string(forKey: "engine") == id)
            assert(AppSettings.voice == voice, "Switching providers lost the saved voice")
        }
        AppSettings.voice = "bf_emma"
        assert(defaults.string(forKey: "voice") == "bf_emma")
        assert(defaults.string(forKey: "cloudflareVoice") == "orion")
        assert(defaults.string(forKey: "geminiVoice") == "Kore")
        withExtendedLifetime(window) {
            player.onPlayStateChange?(true)
            player.onPlayStateChange?(false)
        }

        guard states == [true, false] else {
            fputs("FAIL: opening the player disconnected the startup watchdog observer\n", stderr)
            exit(1)
        }
        print("PASS: opening the player preserves playback start/stop notifications")
        print("PASS: native provider selection preserves local and hosted voice preferences")
    }
}

import Foundation

/// User-facing settings, persisted in UserDefaults. The defaults use the
/// built-in Kokoro voice bm_fable, 1× speed, and calm off.
enum AppSettings {
    private static let defaults = UserDefaults.standard

    static var voice: String {
        get { defaults.string(forKey: "voice") ?? "bm_fable" }
        set { defaults.set(newValue, forKey: "voice") }
    }

    static var speed: Double {
        get {
            let v = defaults.double(forKey: "speed")
            return v == 0 ? 1.0 : v
        }
        set { defaults.set(newValue, forKey: "speed") }
    }

    static var calm: Bool {
        get { defaults.bool(forKey: "calm") }
        set { defaults.set(newValue, forKey: "calm") }
    }
}

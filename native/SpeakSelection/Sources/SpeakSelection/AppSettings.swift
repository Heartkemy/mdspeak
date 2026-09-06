import Foundation

/// User-facing settings, persisted in UserDefaults. The defaults mirror the
/// spec: `bm_lewis`, 1× speed, calm off.
enum AppSettings {
    private static let defaults = UserDefaults.standard

    static var voice: String {
        get { defaults.string(forKey: "voice") ?? "bm_lewis" }
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

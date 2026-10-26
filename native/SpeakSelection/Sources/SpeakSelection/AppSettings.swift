import Foundation

enum SpeechEngine: String, CaseIterable {
    case cloudflare, gemini, kokoro

    var label: String {
        switch self {
        case .cloudflare: return "Cloudflare Aura 2 (sends text)"
        case .gemini: return "Gemini (sends text to Google)"
        case .kokoro: return "Kokoro (local)"
        }
    }

    var voiceKey: String { self == .kokoro ? "voice" : "\(rawValue)Voice" }

    var defaultVoice: String {
        switch self {
        case .cloudflare: return "jupiter"
        case .gemini: return "Charon"
        case .kokoro: return "bm_fable"
        }
    }
}

/// User-facing settings, persisted in UserDefaults. Keep each provider's voice separate.
enum AppSettings {
    private static let defaults = UserDefaults.standard

    static var engine: SpeechEngine {
        get { SpeechEngine(rawValue: defaults.string(forKey: "engine") ?? "") ?? .kokoro }
        set { defaults.set(newValue.rawValue, forKey: "engine") }
    }

    static var voice: String {
        get { defaults.string(forKey: engine.voiceKey) ?? engine.defaultVoice }
        set { defaults.set(newValue, forKey: engine.voiceKey) }
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

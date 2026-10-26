import Foundation

struct SpeakJob {
    let id: String
    let segments: Int
}

/// Owns the long-lived `mdspeak-speak` worker: spawns it, waits for it to be
/// ready, POSTs selected text, and terminates it when the shell quits. The
/// worker is what holds the Kokoro model warm; this shell just talks HTTP.
final class Worker {
    /// A UserDefaults override is intentionally first: it is the escape hatch
    /// for an app launched outside a shell with a configured PATH.
    private static let workerPathKey = "workerPath"

    let port: UInt16
    private var process: Process?

    init(port: UInt16 = 8799) {
        self.port = port
    }

    var executable: String? {
        if let override = UserDefaults.standard.string(forKey: Self.workerPathKey),
           !override.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return Self.executable(at: override)
        }
        if let configured = ProcessInfo.processInfo.environment["MDSPEAK_SPEAK_COMMAND"],
           !configured.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return Self.executable(at: configured)
        }
        return Self.commandOnPath("mdspeak-speak")
    }

    var configurationError: String {
        if let override = UserDefaults.standard.string(forKey: Self.workerPathKey),
           !override.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "workerPath is not executable: " + override
        }
        if let configured = ProcessInfo.processInfo.environment["MDSPEAK_SPEAK_COMMAND"],
           !configured.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "MDSPEAK_SPEAK_COMMAND is not executable: " + configured
        }
        return "Install mdspeak, put mdspeak-speak on PATH, or set MDSPEAK_SPEAK_COMMAND."
    }

    private static func executable(at path: String) -> String? {
        let expanded = (path as NSString).expandingTildeInPath
        return FileManager.default.isExecutableFile(atPath: expanded) ? expanded : nil
    }

    private static func commandOnPath(_ command: String) -> String? {
        let path = ProcessInfo.processInfo.environment["PATH"] ?? ""
        for directory in path.split(separator: ":") {
            let candidate = URL(fileURLWithPath: String(directory)).appendingPathComponent(command).path
            if FileManager.default.isExecutableFile(atPath: candidate) {
                return candidate
            }
        }
        return nil
    }

    // ---------------------------------------------------------------- lifecycle

    func start() {
        guard process == nil else { return }
        guard let executable else {
            NSLog("SpeakSelection: %@", configurationError)
            return
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: executable)
        proc.arguments = ["--port", String(port)]
        proc.terminationHandler = { [weak self] _ in
            self?.process = nil
        }
        do {
            try proc.run()
            process = proc
        } catch {
            process = nil
            NSLog("SpeakSelection: could not start %@: %@", executable, error.localizedDescription)
        }
    }

    func stop() {
        process?.terminate()
        process = nil
    }

    // ---------------------------------------------------------------- requests

    private func url(_ path: String) -> URL? {
        URL(string: "http://127.0.0.1:\(port)\(path)")
    }

    /// POST text; on success the completion gets the job (id + segment count).
    func speak(text: String, voice: String, calm: Bool, completion: @escaping (SpeakJob?) -> Void) {
        guard let url = url("/speak") else { completion(nil); return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "content-type")
        request.httpBody = try? JSONSerialization.data(
            withJSONObject: ["text": text, "voice": voice, "calm": calm]
        )
        request.timeoutInterval = 60

        URLSession.shared.dataTask(with: request) { data, response, _ in
            guard let data,
                  let http = response as? HTTPURLResponse,
                  http.statusCode == 200,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let id = json["id"] as? String,
                  let segments = json["segments"] as? Int else {
                completion(nil)
                return
            }
            completion(SpeakJob(id: id, segments: segments))
        }.resume()
    }

    /// Blocking fetch of one segment's WAV bytes. Call on a background queue —
    /// the worker holds the request until that segment is rendered.
    func fetchSegment(jobId: String, index: Int) -> Data? {
        guard let url = url("/segment/\(jobId)/\(index).wav") else { return nil }
        let semaphore = DispatchSemaphore(value: 0)
        var result: Data?
        var request = URLRequest(url: url)
        request.timeoutInterval = 600
        URLSession.shared.dataTask(with: request) { data, response, _ in
            if let data, let http = response as? HTTPURLResponse, http.statusCode == 200 {
                result = data
            }
            semaphore.signal()
        }.resume()
        semaphore.wait()
        return result
    }

    /// Fetch the curated voice list: `[(id, label)]`.
    func voices(completion: @escaping ([(id: String, label: String)]) -> Void) {
        guard let url = url("/voices") else { completion([]); return }
        URLSession.shared.dataTask(with: url) { data, _, _ in
            guard let data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let list = json["voices"] as? [[String: Any]] else {
                completion([])
                return
            }
            let voices = list.compactMap { item -> (id: String, label: String)? in
                guard let id = item["id"] as? String, let label = item["label"] as? String else { return nil }
                return (id, label)
            }
            completion(voices)
        }.resume()
    }
}

import AppKit
import AVFoundation

/// Owns playback. Long selections are streamed: the worker renders the text in
/// segments, and this class starts playing the first segment as soon as it's
/// ready while the next segments are still rendering. Transport, seek and
/// progress are all expressed in *global* time (across the whole selection).
///
/// Every segment load is asynchronous and guarded by an epoch token: stopping,
/// pausing, seeking, or starting a new run bumps the epoch, and a load that
/// lands on a stale epoch is dropped. Without that a fetch that was in flight
/// when the user hit stop/pause would later start audio anyway — which is what
/// left the speaking indicator on with no sound coming out.
final class Player: NSObject, AVAudioPlayerDelegate {
    private var player: AVAudioPlayer?
    private var timer: Timer?

    // Streaming state
    private var fetchSegment: ((Int) -> Data?)?
    private var totalSegments = 0
    private var currentIndex = -1
    private var durations: [Double] = []
    private var prefetched: [Int: Data] = [:]
    private let prefetchedLock = NSLock()
    private var rate: Float = 1.0

    /// Cancel token. Bumped by stop/pause/seek/new-run; async loads capture it
    /// when they start and are ignored if it changed by the time they land.
    private var epoch = 0

    /// True only while an `AVAudioPlayer` is audibly playing.
    private var speaking = false
    /// True while the user has explicitly paused (as opposed to idle/finished).
    private var paused = false

    /// Audible state — the menu-bar indicator. Called on the main thread.
    var onPlayStateChange: ((Bool) -> Void)?
    /// A committed run lost a segment (load error, worker failure) — it's over.
    var onLoadFailure: (() -> Void)?
    /// Progress tick: `(current, total)` in seconds (player window scrubber).
    var onTick: ((Double, Double) -> Void)?

    var isPlaying: Bool { speaking }
    var currentTime: Double { currentPosition() }
    var duration: Double { totalDuration() }

    // ---------------------------------------------------------------- entry

    func playSegments(total: Int, rate: Float, fetch: @escaping (Int) -> Data?) {
        invalidate()
        let run = epoch
        self.rate = rate
        totalSegments = total
        fetchSegment = fetch
        durations = Array(repeating: 0, count: total)
        prefetchedLock.lock()
        prefetched = [:]
        prefetchedLock.unlock()
        currentIndex = -1
        paused = false
        discardAudio()
        setSpeaking(false)
        loadAndPlay(index: 0, offset: 0, run: run)
    }

    private func playNext() {
        let next = currentIndex + 1
        guard next < totalSegments else {
            finishPlayback()
            return
        }
        loadAndPlay(index: next, offset: 0, run: epoch)
    }

    private func loadAndPlay(index: Int, offset: Double, run: Int) {
        guard run == epoch, index < totalSegments else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let data = self?.dataFor(index, run: run)
            DispatchQueue.main.async {
                guard let self, run == self.epoch else { return } // cancelled while fetching
                guard let data else {
                    self.failRun()
                    return
                }
                self.startSegment(index: index, data: data, offset: offset, run: run)
            }
        }
    }

    private func startSegment(index: Int, data: Data, offset: Double, run: Int) {
        do {
            let url = FileManager.default.temporaryDirectory
                .appendingPathComponent("mdspeak-\(UUID().uuidString).wav")
            try data.write(to: url)
            let p = try AVAudioPlayer(contentsOf: url)
            p.delegate = self
            p.enableRate = true
            p.rate = rate
            p.currentTime = offset
            discardAudio()
            player = p
            currentIndex = index
            durations[index] = p.duration
            p.play()
            startTimer()
            setSpeaking(true)
        } catch {
            failRun()
            return
        }
        prefetch(index + 1, run: run)
    }

    private func dataFor(_ index: Int, run: Int) -> Data? {
        prefetchedLock.lock()
        if let data = prefetched[index] {
            prefetchedLock.unlock()
            return data
        }
        prefetchedLock.unlock()

        guard run == epoch, let data = fetchSegment?(index) else { return nil }
        prefetchedLock.lock()
        if run == epoch {
            prefetched[index] = data
        }
        prefetchedLock.unlock()
        return data
    }

    private func prefetch(_ index: Int, run: Int) {
        guard index < totalSegments, run == epoch else { return }
        prefetchedLock.lock()
        let has = prefetched[index] != nil
        prefetchedLock.unlock()
        guard !has else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let data = self?.fetchSegment?(index) else { return }
            self?.prefetchedLock.lock()
            if self?.epoch == run {
                self?.prefetched[index] = data
            }
            self?.prefetchedLock.unlock()
        }
    }

    private func finishPlayback() {
        discardAudio()
        currentIndex = -1
        setSpeaking(false)
        onTick?(totalDuration(), totalDuration())
    }

    private func failRun() {
        invalidate()
        discardAudio()
        currentIndex = -1
        setSpeaking(false)
        onLoadFailure?()
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        guard flag else {
            failRun()
            return
        }
        guard !paused else { return } // user paused at the boundary; resume() continues
        playNext()
    }

    // ---------------------------------------------------------------- position

    private func offset(of index: Int) -> Double {
        durations.prefix(max(0, index)).reduce(0, +)
    }

    private func currentPosition() -> Double {
        guard currentIndex >= 0 else { return 0 }
        return offset(of: currentIndex) + (player?.currentTime ?? 0)
    }

    private func totalDuration() -> Double {
        durations.reduce(0, +)
    }

    // ---------------------------------------------------------------- controls

    func togglePlayPause() {
        if speaking {
            pause()
        } else if paused {
            resume()
        }
    }

    private func pause() {
        guard speaking else { return }
        invalidate() // drop any in-flight load for the next segment
        paused = true
        player?.pause()
        stopTimer()
        setSpeaking(false)
    }

    private func resume() {
        guard paused, !speaking else { return }
        paused = false
        if let p = player, !isFinished(p) {
            p.play()
            startTimer()
            setSpeaking(true)
        } else if currentIndex >= 0 {
            // Paused right at a segment boundary: the tail already ended while
            // we paused, so continue from the next segment instead of replaying.
            playNext()
        }
    }

    /// True if the loaded segment has run to its end and playback is waiting on
    /// (or has lost) the next one — pausing there would just replay the tail.
    private func isFinished(_ p: AVAudioPlayer) -> Bool {
        p.duration - p.currentTime < 0.05
    }

    func seek(to time: Double) {
        guard time >= 0 else { return }
        var acc = 0.0
        var targetIndex = -1
        var targetOffset = 0.0
        for i in 0..<totalSegments {
            let d = durations[i]
            if d > 0 {
                if acc + d >= time {
                    targetIndex = i
                    targetOffset = time - acc
                    break
                }
                acc += d
            }
        }
        guard targetIndex >= 0 else { return }
        invalidate()
        let run = epoch
        paused = false
        discardAudio()
        setSpeaking(false)
        loadAndPlay(index: targetIndex, offset: targetOffset, run: run)
    }

    func skip(by delta: Double) {
        seek(to: currentPosition() + delta)
    }

    func setRate(_ rate: Float) {
        self.rate = rate
        player?.rate = rate
    }

    func stop() {
        invalidate()
        paused = false
        discardAudio()
        currentIndex = -1
        setSpeaking(false)
    }

    // ---------------------------------------------------------------- internals

    private func invalidate() {
        epoch += 1
    }

    private func discardAudio() {
        player?.stop()
        player = nil
        stopTimer()
    }

    private func setSpeaking(_ value: Bool) {
        guard speaking != value else { return }
        speaking = value
        onPlayStateChange?(value)
    }

    // ---------------------------------------------------------------- timer

    private func startTimer() {
        stopTimer()
        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self, self.speaking else { return }
            self.onTick?(self.currentPosition(), self.totalDuration())
        }
    }

    private func stopTimer() {
        timer?.invalidate()
        timer = nil
    }
}

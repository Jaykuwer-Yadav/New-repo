// Global Birthday Memories JavaScript
// Enhanced with Web Audio Happy Birthday Synthesizer, Confetti & Interactive Features

document.addEventListener("DOMContentLoaded", () => {
    initParticles();
    initPasswordToggles();
    initActiveNav();
});

// 1. Background Particles
function initParticles() {
    const bg = document.getElementById("bgParticles");
    if (!bg) return;
    
    const count = 25;
    for (let i = 0; i < count; i++) {
        const p = document.createElement("div");
        p.className = "particle";
        
        const size = Math.random() * 70 + 20;
        p.style.width = `${size}px`;
        p.style.height = `${size}px`;
        p.style.left = `${Math.random() * 100}%`;
        p.style.animationDuration = `${Math.random() * 12 + 10}s`;
        p.style.animationDelay = `${Math.random() * -15}s`;
        
        bg.appendChild(p);
    }
}

// 2. Password Visibility Toggle
function initPasswordToggles() {
    document.querySelectorAll(".toggle-password-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            const input = btn.previousElementSibling;
            if (!input) return;
            
            if (input.type === "password") {
                input.type = "text";
                btn.textContent = "🙈";
            } else {
                input.type = "password";
                btn.textContent = "👁️";
            }
        });
    });
}

// 3. Highlight current page in Navbar
function initActiveNav() {
    const path = window.location.pathname;
    document.querySelectorAll(".nav-links .nav-item").forEach(item => {
        if (item.getAttribute("href") === path) {
            item.classList.add("active");
        }
    });
}

// 4. Confetti Celebrations Helper
function triggerConfettiBurst(duration = 2.5) {
    if (typeof confetti === "undefined") return;
    
    const end = Date.now() + (duration * 1000);
    const colors = ['#ff4d8d', '#4df0ff', '#ffd64d', '#ffffff', '#a238ff'];

    (function frame() {
        confetti({
            particleCount: 3,
            angle: 60,
            spread: 55,
            origin: { x: 0 },
            colors: colors
        });
        confetti({
            particleCount: 3,
            angle: 120,
            spread: 55,
            origin: { x: 1 },
            colors: colors
        });

        if (Date.now() < end) {
            requestAnimationFrame(frame);
        }
    }());
}

function triggerFireworks(duration = 4.0) {
    if (typeof confetti === "undefined") return;
    
    const end = Date.now() + (duration * 1000);
    const colors = ['#ff4d8d', '#4df0ff', '#ffd64d', '#ffffff', '#ff75c3'];

    const interval = setInterval(function() {
        if (Date.now() > end) {
            return clearInterval(interval);
        }
        confetti({
            startVelocity: 30,
            spread: 360,
            ticks: 60,
            origin: { x: Math.random(), y: Math.random() - 0.2 },
            colors: colors
        });
    }, 200);
}

// 5. Earn Badge Helper API
async function earnBadge(badgeName) {
    try {
        const response = await fetch("/api/earn-badge", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ badge_name: badgeName })
        });
        const result = await response.json();
        if (result.status === "success") {
            console.log(`Badge Earned: ${badgeName}`);
        }
    } catch (err) {
        console.error("Failed to earn badge:", err);
    }
}

// ==========================================
// 6. Polyphonic "Happy Birthday to You" Web Audio Chime Synthesizer
// ==========================================

let musicAudioCtx = null;
let isMusicPlaying = false;
let musicTimeouts = [];
window.isBirthdayMusicPlaying = false;

// Note frequencies (Hz) for Happy Birthday in Key of C
const NOTES = {
    'C4': 261.63, 'D4': 293.66, 'E4': 329.63, 'F4': 349.23,
    'G4': 392.00, 'A4': 440.00, 'B4': 493.88, 'C5': 523.25,
    'D5': 587.33, 'E5': 659.25, 'F5': 698.46, 'G5': 783.99,
    'A5': 880.00, 'Bb4': 466.16
};

// Official Happy Birthday Melody & Timing (Note, Duration in beats)
const BDAY_MELODY = [
    { note: 'G4', dur: 0.75 }, { note: 'G4', dur: 0.25 }, { note: 'A4', dur: 1.0 }, { note: 'G4', dur: 1.0 }, { note: 'C5', dur: 1.0 }, { note: 'B4', dur: 2.0 },
    { note: 'G4', dur: 0.75 }, { note: 'G4', dur: 0.25 }, { note: 'A4', dur: 1.0 }, { note: 'G4', dur: 1.0 }, { note: 'D5', dur: 1.0 }, { note: 'C5', dur: 2.0 },
    { note: 'G4', dur: 0.75 }, { note: 'G4', dur: 0.25 }, { note: 'G5', dur: 1.0 }, { note: 'E5', dur: 1.0 }, { note: 'C5', dur: 1.0 }, { note: 'B4', dur: 1.0 }, { note: 'A4', dur: 1.5 },
    { note: 'F5', dur: 0.75 }, { note: 'F5', dur: 0.25 }, { note: 'E5', dur: 1.0 }, { note: 'C5', dur: 1.0 }, { note: 'D5', dur: 1.0 }, { note: 'C5', dur: 2.5 }
];

function playChimeNote(freq, startTime, duration) {
    if (!musicAudioCtx) return;
    
    // Primary Tone (Chime/Bell Oscillator)
    const osc1 = musicAudioCtx.createOscillator();
    const osc2 = musicAudioCtx.createOscillator();
    const gainNode = musicAudioCtx.createGain();

    osc1.type = 'triangle';
    osc1.frequency.setValueAtTime(freq, startTime);

    // Harmonic overtone for shimmering music-box chime feel
    osc2.type = 'sine';
    osc2.frequency.setValueAtTime(freq * 2, startTime);

    gainNode.gain.setValueAtTime(0.001, startTime);
    gainNode.gain.exponentialRampToValueAtTime(0.3, startTime + 0.04);
    gainNode.gain.exponentialRampToValueAtTime(0.001, startTime + duration * 0.95);

    osc1.connect(gainNode);
    osc2.connect(gainNode);
    gainNode.connect(musicAudioCtx.destination);

    osc1.start(startTime);
    osc2.start(startTime);
    osc1.stop(startTime + duration);
    osc2.stop(startTime + duration);
}

function startBirthdayTune() {
    if (!musicAudioCtx) {
        musicAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (musicAudioCtx.state === 'suspended') {
        musicAudioCtx.resume();
    }

    isMusicPlaying = true;
    window.isBirthdayMusicPlaying = true;
    updateMusicUI(true);
    triggerConfettiBurst(2.0);

    const tempo = 0.48; // seconds per beat
    let curTime = musicAudioCtx.currentTime + 0.1;

    BDAY_MELODY.forEach((item) => {
        const noteDuration = item.dur * tempo;
        playChimeNote(NOTES[item.note], curTime, noteDuration);
        curTime += noteDuration;
    });

    const totalDurationMs = (curTime - musicAudioCtx.currentTime) * 1000;
    
    // Loop after finished
    const loopTimeout = setTimeout(() => {
        if (isMusicPlaying) {
            startBirthdayTune();
        }
    }, totalDurationMs + 800);
    
    musicTimeouts.push(loopTimeout);
}

function stopBirthdayTune() {
    isMusicPlaying = false;
    window.isBirthdayMusicPlaying = false;
    musicTimeouts.forEach(t => clearTimeout(t));
    musicTimeouts = [];
    if (musicAudioCtx && musicAudioCtx.state !== 'closed') {
        musicAudioCtx.suspend();
    }
    updateMusicUI(false);
}

function toggleBirthdayMusic() {
    if (isMusicPlaying) {
        stopBirthdayTune();
    } else {
        startBirthdayTune();
    }
}

function updateMusicUI(isPlaying) {
    const btn = document.getElementById("musicToggleBtn");
    const label = document.getElementById("musicLabel");
    const dashAction = document.getElementById("dashMusicAction");
    
    if (btn) {
        if (isPlaying) {
            btn.classList.add("playing");
            if (label) label.textContent = "Pause Birthday Song ⏸️";
        } else {
            btn.classList.remove("playing");
            if (label) label.textContent = "Happy Birthday Song 🎵";
        }
    }
    if (dashAction) {
        dashAction.textContent = isPlaying ? "Pause Song ⏸️" : "Play Song 🎶";
    }
}

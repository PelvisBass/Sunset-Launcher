"""Upstream build and launch recipes for the launcher library."""

from dataclasses import dataclass, replace
from typing import Literal


@dataclass(frozen=True)
class Step:
    command: tuple[str, ...]
    label: str
    working_directory: str = "{repo}"


@dataclass(frozen=True)
class Option:
    value: str
    label: str
    # Arguments for the launch command, and settings passed like launch_env.
    args: tuple[str, ...] = ()
    settings: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Choice:
    """A game setting the launcher offers as a picker. The first option is the default."""

    key: str
    label: str
    options: tuple[Option, ...]
    tip: str = ""

    def option(self, value: str | None) -> Option:
        return next((option for option in self.options if option.value == value), self.options[0])


@dataclass(frozen=True)
class Recipe:
    dependencies: tuple[str, ...]
    steps: tuple[Step, ...]
    artifact: str
    command: tuple[str, ...]
    runtime: Literal["proton", "native", "managed"]
    requirements: tuple[str, ...] = ()
    working_directory: str = "{repo}"
    prepared_wine_env: tuple[tuple[str, str], ...] = ()
    launch_note: str = ""
    launch_url: str = ""
    rust_target: str = ""
    needs_dxc: bool = False
    needs_xwin_sdk: bool = False
    companion_outputs: tuple[str, ...] = ()
    launch_paths: tuple[tuple[str, str], ...] = ()
    shader_cache: Literal["xcl", "victory", "tuxbox"] | None = None
    # Shown in the Launch options field. {options} in command marks where its arguments go.
    options_hint: str = ""
    launch_env: tuple[tuple[str, str], ...] = ()
    # Console models a run can present, default first: "auto", "series-x" or "series-s". A layer takes the
    # choice as {console_mode} in its command, or through console_env with console_env_values per mode.
    console_modes: tuple[str, ...] = ()
    console_env: str = ""
    console_env_values: tuple[tuple[str, str], ...] = ()
    # Shader warm-up runs like `command`, through the same runtime and cache settings, so a launch
    # finds what it made. {game_dir} is the game's folder and {options} takes the worker count.
    warmup: tuple[str, ...] = ()
    warmup_jobs_arg: str = ""
    warmup_jobs_env: tuple[str, ...] = ()
    # What the upstream command reports: an overall total, a total per game file, a running count, or nothing.
    warmup_progress: Literal["total", "file", "count", "none"] = "none"
    # A second warm-up command: the GPU pipelines earlier runs recorded, made ahead so a run does not stop for
    # them. It can take long, so only a warm-up the user starts runs it, not one before a launch.
    warmup_pipelines: tuple[str, ...] = ()
    # A launch command that replaces the VICTORY_* environment with its own takes its settings as arguments:
    # launch_env and Launch options' VICTORY_* entries are passed as `<settings_arg> NAME=VALUE` before {options}.
    settings_arg: str = ""
    # Environment the layer reads before it applies its settings: always passed as environment, never as settings_arg.
    run_env: tuple[tuple[str, str], ...] = ()
    # Game settings picked in the launcher. An in-game menu that saves the same settings to menu_file (under
    # %LOCALAPPDATA%) would win over them, so its menu_keys are taken out of that file before each launch.
    choices: tuple[Choice, ...] = ()
    menu_file: str = ""
    menu_keys: tuple[str, ...] = ()
    # Under Proton: Microsoft's D3DCompiler beside the artifact, loaded over Wine's own (d3dcompiler_47=n,b).
    native_d3dcompiler: bool = False
    # NVIDIA's Streamline and DLSS files, fetched after the user agrees and named by VICTORY_STREAMLINE_DIR.
    streamline: bool = False


@dataclass(frozen=True)
class Project:
    key: str
    name: str
    description: str
    repository: str
    setup_url: str
    recipes: dict[str, Recipe]
    game_input: Literal["file", "either"]
    accent: str
    upstream_unavailable: str = ""
    # Earlier upstream addresses. A checkout still pointing at one is moved to `repository`.
    previous_repositories: tuple[str, ...] = ()


# XCL passes everything after the game path to the game. It reads its own settings from the environment.
XCL_LAUNCH = ("{artifact}", "run", "{game}", "{options}")
XCL_OPTIONS = "e.g. XCL_NETWORK=1 DXVK_HUD=fps · other arguments go to the game"
XCL_WINDOWS_OPTIONS = "e.g. XCL_NETWORK=1 · other arguments go to the game"

# `xcl shaders --precompile` fills the DXIL cache a run reads, and --pipelines the driver's pipeline cache.
# XCL reads the console from the title's files unless XCL_DEVICE_TYPE overrides it.
XCL_CONSOLE = dict(
    console_modes=("auto", "series-x", "series-s"),
    console_env="XCL_DEVICE_TYPE",
    console_env_values=(("series-x", "anaconda"), ("series-s", "lockhart")),
)
XCL_WARMUP = dict(
    warmup=("{artifact}", "shaders", "{game_dir}", "--precompile", "--pipelines"),
    warmup_jobs_env=("XCL_DXC_THREADS",),
    warmup_progress="total",
)

XCL_RECIPES = {
    "linux": Recipe(
        dependencies=("cargo",),
        steps=(
            Step(
                (
                    "cargo", "build", "--locked", "--release", "--workspace",
                    "--target", "x86_64-pc-windows-gnu", "--target-dir", "target",
                ),
                "Cross-compile XCL for Windows",
            ),
        ),
        artifact="target/x86_64-pc-windows-gnu/release/xcl.exe",
        command=XCL_LAUNCH,
        runtime="proton",
        requirements=(
            "Install Rust with rustup and add the target with "
            "rustup target add x86_64-pc-windows-gnu.",
            "Install the POSIX-thread MinGW-w64 x86-64 toolchain. If needed, set "
            "CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER to x86_64-w64-mingw32-gcc-posix.",
            "Place the same-release Microsoft DXC DLLs, dxcompiler.dll and dxil.dll, beside xcl.exe.",
        ),
        prepared_wine_env=(
            ("WINEDLLOVERRIDES", "d3d12,d3d12core,dxgi,d3d11,d3d10core=n,b"),
            ("VKD3D_CONFIG", "ignore_shared_fence"),
            ("XCL_NETWORK", "0"),
            ("WINEDEBUG", "-all"),
        ),
        options_hint=XCL_OPTIONS,
        launch_note="Stock Proton can fail before rendering. Under Wine prefix, you can select a dedicated "
        "prefix with DXVK and an XCL-compatible vkd3d-proton build.",
        rust_target="x86_64-pc-windows-gnu",
        shader_cache="xcl",
        **XCL_WARMUP,
        **XCL_CONSOLE,
    ),
    "windows": Recipe(
        dependencies=("cargo",),
        steps=(
            Step(
                ("cargo", "build", "--locked", "--release", "--workspace", "--target-dir", "target"),
                "Build XCL for Windows",
            ),
        ),
        artifact="target/release/xcl.exe",
        command=XCL_LAUNCH,
        runtime="native",
        options_hint=XCL_WINDOWS_OPTIONS,
        requirements=(
            "Install Rust with the x64 MSVC toolchain, Visual Studio C++ Build Tools and the Windows SDK.",
            "XCL requires a D3D12 GPU with mesh shaders and Shader Model 6.6, DXC and Windows Gaming Services.",
        ),
        shader_cache="xcl",
        **XCL_WARMUP,
        **XCL_CONSOLE,
    ),
}


# Victory accepts its flags anywhere and passes arguments after -- to the title.
VICTORY_LAUNCH = ("{artifact}", "run", "{game}", "--console", "{console_mode}", "--execute", "{options}")
# Victory and its forks default to Series X.
VICTORY_CONSOLES = ("series-x", "series-s")
VICTORY_OPTIONS = "e.g. --unimplemented zero · put -- before arguments for the game"

# `victory translate --cache` is upstream's warm-up. Its --pipelines step is left out: under Proton
# it has no pipeline library to keep pipelines in, and it aborts with exit code 3.
VICTORY_WARMUP = dict(
    warmup=("{artifact}", "translate", "{game_dir}", "--cache", "{options}"),
    warmup_jobs_arg="--jobs",
    warmup_progress="file",
)
# Forks without upstream's warm-up. hyphennn's needs a list of shader hashes from an earlier run.
NO_WARMUP = dict(warmup=(), warmup_jobs_arg="", warmup_jobs_env=(), warmup_progress="none", warmup_pipelines=())
# Cool Fox and its successors translate every shader with `victory shaders` and report a running count.
FORK_WARMUP = dict(
    warmup=("{artifact}", "shaders", "{game_dir}", "{options}"),
    warmup_jobs_arg="--jobs",
    warmup_progress="count",
    # `victory warm`: every pipeline earlier runs wrote down, made in processes of its own. It keeps what it made
    # per translator and driver, so a later one adds only new pipelines until an update changes the translator.
    warmup_pipelines=("{artifact}", "warm", "{options}"),
)

VICTORY_RECIPES = {
    "linux": Recipe(
        dependencies=("cargo", "cargo-xwin", "clang-cl", "lld-link"),
        steps=(
            Step(
                (
                    "cargo", "xwin", "build", "--locked", "--release", "--target",
                    "x86_64-pc-windows-msvc", "--target-dir", "target", "-p", "victory",
                ),
                "Cross-compile Victory for Windows",
            ),
        ),
        artifact="target/x86_64-pc-windows-msvc/release/victory.exe",
        command=VICTORY_LAUNCH,
        runtime="proton",
        requirements=(
            "Install Rust with rustup, then install LLVM with clang-cl and lld-link.",
            "Run cargo install --locked cargo-xwin. The repository selects its own Rust toolchain and Windows target.",
        ),
        options_hint=VICTORY_OPTIONS,
        launch_note="Linux compatibility is experimental.",
        rust_target="x86_64-pc-windows-msvc",
        shader_cache="victory",
        **VICTORY_WARMUP,
        console_modes=VICTORY_CONSOLES,
        # Wine's compiler cannot build some of the title's shaders, which shows in Series S mode.
        native_d3dcompiler=True,
    ),
    "windows": Recipe(
        dependencies=("cargo",),
        steps=(
            Step(
                ("cargo", "build", "--locked", "--release", "--target-dir", "target", "-p", "victory"),
                "Build Victory for Windows",
            ),
        ),
        artifact="target/release/victory.exe",
        command=VICTORY_LAUNCH,
        runtime="native",
        requirements=("Install Rust with rustup, Visual Studio C++ Build Tools and the Windows SDK.",),
        options_hint=VICTORY_OPTIONS,
        shader_cache="victory",
        **VICTORY_WARMUP,
        console_modes=VICTORY_CONSOLES,
    ),
}


TUXBOX_STEPS = (
    Step(("{python}", "-m", "venv", "{venv}"), "Set up the TuxBox Python environment"),
    Step(
        ("{venv_python}", "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"),
        "Install TuxBox Python dependencies",
    ),
    Step(("{venv_python}", "build.py"), "Build TuxBox providers and the Lanes demo"),
)
# TuxBox passes everything after the game path to the game, so its options come first.
TUXBOX_LAUNCH = ("{venv_python}", "tuxbox.py", "run", "{options}", "{game}")
TUXBOX_OPTIONS = "e.g. --mangohud --offline --no-shader-prep --graphics-menu"

TUXBOX_ARTIFACT = "build/lanes/lanes.exe"
TUXBOX_PROVIDERS = (
    "build/xmem-smoke/xmem.dll", "build/xmem-smoke/tuxbox_xhttp.dll",
    "build/d3d12-smoke/d3d12_xs.dll", "build/xg-provider/xg_xs.dll",
    "build/xapu/xapu.dll", "build/xapu/chad.dll", "build/acphal/AcpHal.dll",
    "build/xcrdapi/xcrdapi.dll", "build/gameinput/GameInput.dll",
    "build/xgameruntime/xgameruntime.dll", "build/bvh/XboxBvhBuilder.dll",
    "build/dxc-utils/dxcompiler_xs.dll",
    "build/lanes/xmem.dll", "build/lanes/tuxbox_xhttp.dll",
    "build/lanes/d3d12_xs.dll", "build/lanes/GameInput.dll",
)
TUXBOX_RUST = "Install Rust 1.95 or newer and run rustup target add x86_64-pc-windows-msvc."
TUXBOX_DXC = "Install Microsoft's dxc on PATH or set DXC to its executable."
TUXBOX_STAGING = "Some titles need the built providers staged in a working copy before launching."
TUXBOX_LAUNCH_GUIDE = "https://github.com/brcly/TuxBox/blob/main/examples/lanes/README.md#build-and-play"
TUXBOX_RECIPES = {
    "linux": Recipe(
        dependencies=("cargo", "clang-cl", "lld-link"),
        steps=TUXBOX_STEPS,
        artifact=TUXBOX_ARTIFACT,
        command=TUXBOX_LAUNCH,
        runtime="managed",
        requirements=(
            TUXBOX_RUST,
            "Install clang-cl, lld-link and Python's venv support. Run cargo install --locked cargo-xwin "
            "and cargo xwin cache xwin --xwin-arch x86_64 once, or set XWIN_SDK_ROOT to an existing xwin tree.",
            TUXBOX_DXC,
            "For launch, install umu-run, Steam's Proton Experimental, a C compiler and Vulkan headers.",
        ),
        launch_note=f"TuxBox manages Proton on Linux. {TUXBOX_STAGING}",
        options_hint=TUXBOX_OPTIONS,
        launch_url=TUXBOX_LAUNCH_GUIDE,
        rust_target="x86_64-pc-windows-msvc",
        needs_dxc=True,
        needs_xwin_sdk=True,
        companion_outputs=TUXBOX_PROVIDERS,
        shader_cache="tuxbox",
        # TuxBox prepares shaders with its own per-title profiles and reports no counts.
        warmup=("{venv_python}", "tuxbox.py", "run", "--prepare-only", "{game}"),
    ),
    # TuxBox targets Linux. Windows users can run the upstream project directly.
}


VICTORY_LAUNCH_PATHS = (("VICTORY_CACHE_DIR", "cache"), ("VICTORY_DATA_DIR", "data"))


DEXNOM_REPOSITORY = "https://github.com/DexNom/VIctory"
DEXNOM_RECIPES = {
    host: replace(
        recipe,
        dependencies=(*recipe.dependencies, powershell),
        steps=(
            Step(
                (powershell, "-NoProfile", "-NonInteractive", *policy, "-File", "Initialize-FormatTables.ps1"),
                "Download and verify DexNom format tables",
            ),
            Step((*recipe.steps[0].command, "--features", "diagnostics", "--bin", "victory"), "Build DexNom Runtime"),
        ),
        command=("{artifact}", "run", "{game}", "--execute", "--console", "{console_mode}", "--unimplemented", "trap", "{options}"),
        launch_paths=VICTORY_LAUNCH_PATHS,
        **NO_WARMUP,
        options_hint="Put -- before arguments for the game",
        requirements=(*recipe.requirements, "Install PowerShell (pwsh) to initialize the format tables.")
        if host == "linux" else recipe.requirements,
        launch_note=("Proton launch is experimental. Upstream targets Windows x64."
                     if host == "linux" else "Upstream recommends a current Direct3D 12 driver with mesh shaders and Shader Model 6.6."),
    )
    for host, recipe, powershell, policy in (
        ("linux", VICTORY_RECIPES["linux"], "pwsh", ()),
        ("windows", VICTORY_RECIPES["windows"], "powershell.exe", ("-ExecutionPolicy", "RemoteSigned")),
    )
}


# The original codec-d/cool_fox repository is unavailable. This mirror has the same history.
COOL_FOX_REPOSITORY = "https://github.com/DoubleGhosty/cool_fox"
COOL_FOX_PREVIOUS = ("https://github.com/codec-d/cool_fox",)
# Renderer settings from upstream's Play-VIctory.cmd (pareto-residency). Upstream keeps THIN and JOIN_PARTS off on
# NVIDIA, where they leave skin undrawn and repeat video frames, and the renderer ports off on 8 GB cards.
COOL_FOX_LAUNCH_ENV = (
    ("VICTORY_AMBIENT_AIR", "1.5"), ("VICTORY_TEXTURE_NO_WAIT", "1"), ("VICTORY_RELAY", "2"),
    ("VICTORY_WIDE_BACK", "1"), ("VICTORY_CARD_REHASH_RUNS", "1"), ("VICTORY_JOIN_COPIES", "1"),
    ("VICTORY_INDIRECT_WINDOW", "1"), ("VICTORY_RELAY_COPY", "1"), ("VICTORY_CARD_STEPS", "2"),
    ("VICTORY_INDIRECT_BATCH", "2"), ("VICTORY_MAP_THREADS", "1"), ("VICTORY_CARD_AWAKE", "1"),
    ("VICTORY_SHOW_AFTER", "1"), ("VICTORY_AMBIENT", "constant:0.8"), ("VICTORY_TEXTURE_GUARD", "0"),
    ("VICTORY_THIN", "0"), ("VICTORY_JOIN_PARTS", "0"), ("VICTORY_PORT_CONSTANTS", "0"), ("VICTORY_PORT_CBV", "0"),
    ("VICTORY_LIBRARY_STORE_LATER", "1"), ("VICTORY_PIPELINE_WAIT_MS", "0"), ("VICTORY_UPLOAD_AHEAD", "1"),
    ("VICTORY_LEVELS_ON_DEMAND", "1"), ("VICTORY_CARD_BUFFERS_MIN_KB", "64"), ("VICTORY_LOG", "warn"),
)
COOL_FOX_RECIPES = {
    host: replace(
        recipe,
        steps=(replace(recipe.steps[0], label="Build Cool Fox"),),
        command=("{artifact}", "run", "{game}", "--console", "{console_mode}", "--execute", "{options}"),
        launch_paths=VICTORY_LAUNCH_PATHS,
        **FORK_WARMUP,
        launch_env=COOL_FOX_LAUNCH_ENV,
        # Upstream starts Series X: on an 8 GB card Series S leaves out faces and skin.
        console_modes=VICTORY_CONSOLES,
        options_hint="Put -- before arguments for the game",
        launch_note=(
            "Uses the upstream pareto-residency settings. Proton launch is experimental."
            if host == "linux" else "Uses the upstream pareto-residency settings."
        ),
    )
    for host, recipe in VICTORY_RECIPES.items()
}


SCARLETT_REPOSITORY = "https://github.com/Yaekai/victory-scarlett-3070"
# Settings from the fork's scripts/play-3070.ps1 with its defaults: Series S, mip drop 1, 1024 MB card buffers,
# 512 MB card windows and no ray tracing. The script also sets VICTORY_GAME_DIR, which `run` does not need.
SCARLETT_LAUNCH_ENV = (
    ("VICTORY_PIPELINE_BOOK", "1"), ("VICTORY_LOG", "info"), ("VICTORY_FRAME_INTERVAL_MS", "16.667"),
    # 8 GB VRAM budget and page pool
    ("VICTORY_DYNAMIC_SLOTS", "524288"), ("VICTORY_MAX_PRELUDES", "32"), ("VICTORY_CARD_BUFFERS_MB", "1024"),
    ("VICTORY_CARD_WINDOWS_MB", "512"), ("VICTORY_TEXTURE_MIP_DROP", "1"), ("VICTORY_TEXTURE_ROOM_PERCENT", "94"),
    ("VICTORY_PAGE_POOL", "1"), ("VICTORY_STAGING_PACK", "1"), ("VICTORY_LEVELS_ON_DEMAND", "0"),
    # Submission and upload
    ("VICTORY_WAIT_RESIDENT", "0"), ("VICTORY_JOIN_COPIES", "0"), ("VICTORY_REFRESH_ONCE", "0"),
    ("VICTORY_CARD_REHASH_RUNS", "1"), ("VICTORY_BACK_OWN_VIEW", "1"), ("VICTORY_WIDE_MERGE", "1"),
    ("VICTORY_MIRROR_FAST", "1"), ("VICTORY_PIPELINE_WAIT_MS", "20"), ("VICTORY_NO_WATCHDOG", "1"),
    ("VICTORY_ASYNC_SUBMIT", "1"), ("VICTORY_QUEUE2", "1"), ("VICTORY_INDIRECT_WINDOW", "1"),
    ("VICTORY_TEXTURE_NO_WAIT", "1"), ("VICTORY_COPY_PLAIN", "1"), ("VICTORY_WIDE_TEXTURES", "1"),
    ("VICTORY_INDIRECT_GATHER", "1"), ("VICTORY_LATE_WAIT_LISTS", "1024"), ("VICTORY_CARD_FILLS_BATCH", "0"),
    ("VICTORY_GATHER_DISPATCH", "0"), ("VICTORY_RELAY", "2"), ("VICTORY_WIDE_BACK", "1"),
    ("VICTORY_RELAY_COPY", "1"), ("VICTORY_CARD_STEPS", "2"), ("VICTORY_INDIRECT_BATCH", "2"),
    ("VICTORY_MAP_THREADS", "1"), ("VICTORY_CARD_AWAKE", "0"), ("VICTORY_SHOW_AFTER", "1"),
    ("VICTORY_JOIN_PARTS", "8"), ("VICTORY_UPLOAD_AHEAD", "1"), ("VICTORY_CARD_BUFFERS_MIN_KB", "64"),
    ("VICTORY_POOL_DISCARD", "1"), ("VICTORY_FILL_TITLE", "1"), ("VICTORY_PARTIAL_DELTA", "1"),
    ("VICTORY_BAKE_DELTA", "1"), ("VICTORY_MAP_LOCK_FINE", "1"), ("VICTORY_POOL_CAP", "1"),
    ("VICTORY_TABLE_MEMO", "1"), ("VICTORY_LIST_ALIAS_EXACT", "1"), ("VICTORY_LIST_HOLD", "1"),
    ("VICTORY_EARLY_TEXTURES_MS", "40"), ("VICTORY_BAKE_TEXTURES_MS", "10000"), ("VICTORY_HEAP_PASS_MAX", "0"),
    ("VICTORY_HEAP_CHANGED_MAX", "0"),
    # A decal-heap defrag pass and the water's two tiled light programs, which leave dark tiles on NVIDIA
    ("VICTORY_LEAVE_OUT", "02384a60fda6f2d0,5dc3b25986622949,6f225b641c5b0708"),
    ("VICTORY_WC_NO_BACK", "1"), ("VICTORY_WC_NO_BACK_MIN_KB", "1024"), ("VICTORY_SYNC_IN_PLACE", "1"),
    ("VICTORY_SPANS_QUICK", "1"), ("VICTORY_SYNC_FINE", "1"), ("VICTORY_HEAP_SYNC_FIRST", "1"),
    ("VICTORY_HEAP_SYNC_AT_SUBMIT", "1"), ("VICTORY_SOFT_VIEWS", "copies"), ("VICTORY_SAMPLERS_MADE", "1"),
    ("VICTORY_TABLE_NAMED_ONLY", "1"), ("VICTORY_QUEUE_MARKS", "1"), ("VICTORY_PORT_CONSTANTS", "1"),
    ("VICTORY_EARLY_DEPTH", "1"), ("VICTORY_COPY_BESIDE", "1"), ("VICTORY_STORE_BESIDE", "1"),
    ("VICTORY_GPU_BACK_MAX_KB", "1024"),
    # Lighting and reflection stand-ins while ray tracing is off
    ("VICTORY_AMBIENT", "0.8"), ("VICTORY_AMBIENT_AIR", "1.5"), ("VICTORY_AMBIENT_SPECULAR", "1"),
)
SCARLETT_RECIPES = {
    host: replace(
        recipe,
        steps=(replace(recipe.steps[0], label="Build Scarlett 3070"),),
        launch_paths=VICTORY_LAUNCH_PATHS,
        **FORK_WARMUP,
        launch_env=SCARLETT_LAUNCH_ENV,
        console_modes=("series-s", "series-x"),
        options_hint="e.g. VICTORY_TEXTURE_MIP_DROP=0 · put -- before arguments for the game",
        launch_note=(
            "Tuned for 8 GB NVIDIA cards. On 12 GB or more, add VICTORY_TEXTURE_MIP_DROP=0 for full-resolution "
            "textures, and choose Series X on a faster GPU." + (" Proton launch is experimental." if host == "linux" else "")
        ),
    )
    for host, recipe in VICTORY_RECIPES.items()
}


NVIDIA_RT_REPOSITORY = "https://github.com/Kemerd/VIctory-NVIDIA-RT"
# PLAY.cmd only builds `victory.exe play` arguments, so the launcher runs `play` itself. `play` sets its own
# switches and drops other VICTORY_* variables, so the preset goes in as --set. Resolution, DLSS, frame
# generation, HDR and the console are left to the in-game F1 menu, which saves them.
# PLAY.cmd's "fast" speed setting, its recommended one.
NVIDIA_RT_FAST = (
    ("VICTORY_PIPELINE_WAIT_ONCE", "1"), ("VICTORY_PIPELINE_WAIT_BACKLOG", "64"),
    ("VICTORY_PIPELINE_SLOW_WORKERS", "8"), ("VICTORY_TEXTURE_LOOK_ONCE", "1"), ("VICTORY_TEXTURE_READ_AHEAD", "1"),
    ("VICTORY_STAGING_NO_ZERO", "1"), ("VICTORY_SEND_COPIED_FIND", "1"), ("VICTORY_FILES_BUFFERED", "1"),
    ("VICTORY_MEMORY_FRESH", "1"), ("VICTORY_MEMORY_ARENAS_AHEAD", "1"), ("VICTORY_MEMORY_QUIET", "1"),
    ("VICTORY_MEMORY_LAND_SENT", "1"), ("VICTORY_MEMORY_ASK_RUNS", "1"), ("VICTORY_MEMORY_LEAN", "1"),
    ("VICTORY_MEMORY_DIRTY", "1"), ("VICTORY_MEMORY_BUFFER_FAULTS", "1"), ("VICTORY_MEMORY_FAULT_PAGES", "40000"),
    ("VICTORY_REFRESH_PROBE_US", "300"), ("VICTORY_DECLARE_ACROSS_LISTS", "1"), ("VICTORY_REFRESH_PROBE_TEXTURES", "1"),
    ("VICTORY_REFRESH_SPLIT", "1"), ("VICTORY_WAIT_SPLIT", "1"), ("VICTORY_DSTORAGE_STATS", "1"),
    ("VICTORY_PIPELINE_PIECES", "1"), ("VICTORY_PIPELINE_PIECES_REFUSE_UNTRANSLATED", "1"),
    ("VICTORY_PIPELINE_PIECES_ONLY", "graphics"), ("VICTORY_REFRESH_SET", "1"), ("VICTORY_TITLE_WRITE_RANGE", "1"),
    ("VICTORY_UNDER_PARTS", "1"), ("VICTORY_SEND_THREAD", "1"),
)
# PLAY.cmd's ten questions, as `play` options and switches. Ray tracing is a choice of its own here.
NVIDIA_RT_CHOICES = (
    Choice("console", "Console", (
        Option("series-x", "Series X"), Option("series-s", "Series S", args=("--series-s",)),
    ), tip="Series S renders at 1280x720 inside: faster and much softer. For weaker cards."),
    Choice("resolution", "Resolution", (
        Option("1440", "2560 × 1440", args=("--res", "1440")),
        Option("1080to1440", "1080p upscaled to 1440p", args=("--res", "1080to1440")),
        Option("4k", "3840 × 2160 (4K)", args=("--res", "4k")),
        Option("wide", "5120 × 1440 (32:9)", args=("--res", "wide")),
        Option("ultrawide", "3440 × 1440 (21:9)", args=("--res", "ultrawide")),
    ), tip="The output size. Series X only: Series S keeps its own."),
    Choice("window", "Window", (
        Option("full", "Fullscreen", settings=(("VICTORY_FULLSCREEN", "1"),)),
        Option("window", "Windowed", settings=(("VICTORY_FULLSCREEN", "0"),)),
    )),
    Choice("dlss", "DLSS", (
        Option("quality", "Quality", args=("--dlss", "quality")),
        Option("balanced", "Balanced", args=("--dlss", "balanced")),
        Option("performance", "Performance", args=("--dlss", "performance")),
        Option("dlaa", "DLAA (native, slowest)", args=("--dlss", "dlaa")),
        Option("off", "Off (the game's upscaling)", args=("--dlss", "off")),
    ), tip="Needs NVIDIA's DLSS files, which the launcher offers to download when it builds."),
    Choice("framegen", "Frame generation", (
        Option("ready", "Ready, switched on with F1"),
        *(Option(str(times), f"{times}x", args=("--framegen", str(times))) for times in range(2, 7)),
        Option("none", "Not loaded", args=("--no-framegen-ready",)),
    ), tip="DLSS frame generation needs an RTX 40 or later; 3x to 6x need an RTX 50. Not loaded leaves a little "
           "more memory for textures."),
    Choice("reflex", "NVIDIA Reflex", (
        Option("boost", "On + boost", settings=(("VICTORY_REFLEX", "boost"),)),
        Option("on", "On", settings=(("VICTORY_REFLEX", "on"),)),
        Option("off", "Off"),
    ), tip="Lower input lag. Frame generation turns it on anyway."),
    Choice("hdr", "HDR10", (
        Option("off", "Off", settings=(("VICTORY_HDR", "0"),)),
        Option("on", "On", settings=(("VICTORY_HDR", "1"),)),
    ), tip="Turn HDR on in the system first."),
    Choice("fps", "Frame cap", tuple(
        Option(cap, label, args=("--fps", cap))
        for cap, label in (("1000", "None"), ("30", "30"), ("60", "60"), ("120", "120"), ("144", "144"))
    )),
    Choice("interface", "Ultrawide interface", (
        Option("band", "16:9 in the middle"),
        Option("stretch", "Stretched", settings=(("VICTORY_UI_LETTERBOX", "0"),)),
    ), tip="Menus and the interface on 21:9 and 32:9 screens. 16:9 screens see no difference."),
    Choice("speed", "Speed", (
        Option("fast", "Fast", settings=NVIDIA_RT_FAST),
        Option("plain", "Plain"),
    ), tip="Fast: pipelines from pieces and a parallel submitting thread. Plain: the upstream release's settings."),
    Choice("rt", "Ray tracing", (
        Option("off", "Off"),
        Option("on", "On (experimental)", settings=(("VICTORY_RT_NATIVE", "1"),)),
    ), tip="This fork's native ray tracing: the game's own, on the RTX cores (VICTORY_RT_NATIVE). Experimental, and "
           "about 5 to 8 fps for now. The older path, the F1 menu's \"RAY TRACING (SLOW, BROKEN)\", is turned off."),
)
# What the F1 menu saves for these settings; PLAY.cmd takes the same out before each start. The menu's older
# "RAY TRACING (SLOW, BROKEN)", VICTORY_BVH_BUILDER, goes too: the Ray tracing choice is the fork's native path,
# VICTORY_RT_NATIVE, and Off should mean neither.
NVIDIA_RT_MENU_KEYS = (
    "VICTORY_MENU_RES", "VICTORY_MENU_DLSS", "VICTORY_MENU_FRAMEGEN", "VICTORY_MENU_FPS", "VICTORY_MENU_CONSOLE",
    "VICTORY_HDR", "VICTORY_DISPLAY_WIDE", "VICTORY_RT_NATIVE", "VICTORY_BVH_BUILDER", "VICTORY_UI_LETTERBOX",
    "VICTORY_FULLSCREEN", "VICTORY_REFLEX",
)
# PLAY.cmd's loading fix, always on.
NVIDIA_RT_LAUNCH_ENV = (("VICTORY_PIPELINE_SAMPLER_TABLE", "1"),)
NVIDIA_RT_RECIPES = {
    host: replace(
        recipe,
        steps=(replace(recipe.steps[0], label="Build VIctory-NVIDIA-RT"),),
        command=("{artifact}", "play", "{game}", "{options}"),
        settings_arg="--set",
        streamline=True,
        # The first start of a new build plays briefly, then restarts itself through PowerShell, which Proton
        # does not have: the game would just close. A later start runs at full speed anyway.
        launch_env=NVIDIA_RT_LAUNCH_ENV + ((("VICTORY_WARMUP_RESTART", "0"),) if host == "linux" else ()),
        launch_paths=(("VICTORY_CACHE_DIR", "cache"), ("VICTORY_DATA_DIR", "data")),
        # Proton reports Windows 10, and `play` then offers Microsoft's Direct3D 12 runtime for Windows 10's own,
        # which cannot run the game's shaders. Proton's Direct3D 12 is vkd3d-proton (shader model 6.9 on an RTX 5070),
        # which neither needs nor loads it. The fork's own stand-in for the version skips the offer; its shader
        # model check still runs. A Windows 11 build number.
        # Proton's NVAPI has no NvAPI_D3D_SetReflexSync, so with frame generation Streamline logs an error for it
        # every frame (tens of thousands a session). `streamline=off` drops Streamline's own messages only; the
        # fork's lines about it, such as "DLSS loaded", stay. VICTORY_LOG=info in Launch options brings them back.
        run_env=(("VICTORY_WINDOWS_BUILD", "22631"), ("VICTORY_LOG", "info,streamline=off")) if host == "linux" else (),
        **FORK_WARMUP,
        console_modes=(),
        choices=NVIDIA_RT_CHOICES,
        menu_file="victory/victory-settings.txt",
        menu_keys=NVIDIA_RT_MENU_KEYS,
        options_hint="e.g. VICTORY_X=1 · other play arguments",
        launch_note=(
            "Game settings here win over the same ones in the F1 menu, which still sets everything else. "
            + "DLSS and frame generation use NVIDIA's files, which the launcher offers to download when it builds. "
            + ("The first start after a build runs slowly; start it again. F1 changes that restart the game "
               "close it under Proton instead, so start it again." if host == "linux" else "")
        ),
    )
    for host, recipe in VICTORY_RECIPES.items()
}


PROJECTS = (
    Project(
        key="xcl",
        name="XCL",
        description="Xbox Series compatibility layer with an RDNA2 shader recompiler.",
        repository="https://github.com/xcl-dev/xcl",
        setup_url="https://github.com/xcl-dev/xcl#readme",
        recipes=XCL_RECIPES,
        game_input="either",
        accent="#f792b8",
    ),
    Project(
        key="victory",
        name="Victory",
        description="Xbox Series compatibility layer with a Direct3D 12 backend.",
        repository="https://github.com/Marvisak/VIctory",
        setup_url="https://rustup.rs/",
        recipes=VICTORY_RECIPES,
        game_input="file",
        accent="#ffab83",
    ),
    Project(
        key="tuxbox",
        name="TuxBox",
        description="Xbox Series compatibility layer with its own game runner and shader preparation.",
        repository="https://github.com/brcly/TuxBox",
        setup_url="https://github.com/brcly/TuxBox/blob/main/examples/lanes/README.md#build-requirements",
        recipes=TUXBOX_RECIPES,
        game_input="either",
        accent="#80d8c8",
    ),
    Project(
        key="hyphennn",
        name="hyphennn",
        description="Victory fork with work on rendering and gameplay compatibility.",
        repository="https://github.com/fxceless/victory-fork",
        setup_url="https://github.com/fxceless/victory-fork#readme",
        recipes={host: replace(recipe, **NO_WARMUP) for host, recipe in VICTORY_RECIPES.items()},
        game_input="file",
        accent="#b9a3f8",
    ),
    Project(
        key="dexnom",
        name="DexNom Runtime",
        description="Victory fork focused on AMD Radeon rendering and GPU memory improvements.",
        repository=DEXNOM_REPOSITORY,
        setup_url=f"{DEXNOM_REPOSITORY}#run-it-on-your-pc",
        recipes=DEXNOM_RECIPES,
        game_input="file",
        accent="#fa9690",
    ),
    Project(
        key="cool_fox",
        name="Cool Fox",
        description="Victory fork with graphics and memory residency tuning.",
        repository=COOL_FOX_REPOSITORY,
        setup_url=f"{COOL_FOX_REPOSITORY}#run-the-codec_d-windows-build",
        recipes=COOL_FOX_RECIPES,
        previous_repositories=COOL_FOX_PREVIOUS,
        game_input="file",
        accent="#fbc590",
    ),
    Project(
        key="scarlett_3070",
        name="Scarlett 3070",
        description="Victory fork tuned for 8 GB NVIDIA cards such as the RTX 3070.",
        repository=SCARLETT_REPOSITORY,
        setup_url=f"{SCARLETT_REPOSITORY}#build-and-run-quick-start",
        recipes=SCARLETT_RECIPES,
        game_input="file",
        accent="#9fe0a8",
    ),
    Project(
        key="nvidia_rt",
        name="VIctory-NVIDIA-RT",
        description="VIctory-NVIDIA fork with faster rendering, 32:9 and HDR10, and work toward the title's ray tracing.",
        repository=NVIDIA_RT_REPOSITORY,
        setup_url=f"{NVIDIA_RT_REPOSITORY}#the-launcher-playcmd",
        recipes=NVIDIA_RT_RECIPES,
        game_input="file",
        accent="#b7e36a",
    ),
)

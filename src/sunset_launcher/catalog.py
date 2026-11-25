"""Upstream build and launch recipes for the launcher library."""

from dataclasses import dataclass, replace
from typing import Literal


@dataclass(frozen=True)
class Step:
    command: tuple[str, ...]
    label: str
    working_directory: str = "{repo}"


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


# XCL passes everything after the game path to the game. It reads its own settings from the environment.
XCL_LAUNCH = ("{artifact}", "run", "{game}", "{options}")
XCL_OPTIONS = "e.g. XCL_NETWORK=1 DXVK_HUD=fps · other arguments go to the game"
XCL_WINDOWS_OPTIONS = "e.g. XCL_NETWORK=1 · other arguments go to the game"

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
    ),
}


# Victory accepts its flags anywhere and passes arguments after -- to the title.
VICTORY_LAUNCH = ("{artifact}", "run", "{game}", "--execute", "{options}")
VICTORY_OPTIONS = "e.g. --console series-s · put -- before arguments for the game"

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
        launch_note="Linux compatibility is experimental. Some titles may need a native Microsoft D3DCompiler DLL "
        "in the Proton prefix.",
        rust_target="x86_64-pc-windows-msvc",
        shader_cache="victory",
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
    ),
    # TuxBox targets Linux. Windows users can run the upstream project directly.
}


ZTRX_REPOSITORY = "https://github.com/Zettrox/ztrx"
ZTRX_LAUNCH_PATHS = (("VICTORY_CACHE_DIR", "cache"), ("VICTORY_DATA_DIR", "data"))
ZTRX_RECIPES = {
    "linux": replace(
        VICTORY_RECIPES["linux"],
        steps=(replace(VICTORY_RECIPES["linux"].steps[0], label="Cross-compile ZTRX for Windows"),),
        launch_paths=ZTRX_LAUNCH_PATHS,
        launch_note="Proton launch is experimental. Upstream documents Windows with an NVIDIA RTX GPU.",
        launch_url=f"{ZTRX_REPOSITORY}#requirements",
    ),
    "windows": replace(
        VICTORY_RECIPES["windows"],
        steps=(replace(VICTORY_RECIPES["windows"].steps[0], label="Build ZTRX for Windows"),),
        launch_paths=ZTRX_LAUNCH_PATHS,
        launch_note="Upstream requires an AVX2 CPU and an NVIDIA RTX 20-series or newer GPU.",
        launch_url=f"{ZTRX_REPOSITORY}#requirements",
    ),
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
        recipes=VICTORY_RECIPES.copy(),
        game_input="file",
        accent="#b9a3f8",
    ),
    Project(
        key="ztrx",
        name="ZTRX",
        description="Victory-based Xbox Series compatibility layer focused on NVIDIA graphics.",
        repository=ZTRX_REPOSITORY,
        setup_url=f"{ZTRX_REPOSITORY}#build",
        recipes=ZTRX_RECIPES,
        game_input="file",
        accent="#f1cd86",
    ),
)

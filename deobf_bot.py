import discord
import asyncio
import aiohttp
import tempfile
import os
import io
import json
import pathlib

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN        = "MTQ1OTc0MDY1NjMyMTY5NTc0NA.GXTa1a.xeQ-lt9pKEbksk0ne7Q6qIGXyfV7ulJc1_ZPpc"
LOADING_EMOJI_ID = 1472582923374035025
MOONSEC_DLL      = "./out/MoonsecDeobfuscator.dll"
DOTNET_PATH      = "dotnet"
# Auto-find unluac jar in the same directory as this script
import glob as _glob
_script_dir = os.path.dirname(os.path.abspath(__file__))
_jar_matches = _glob.glob(os.path.join(_script_dir, "*unluac*.jar"))
UNLUAC_JAR = _jar_matches[0] if _jar_matches else os.path.join(_script_dir, "unluac.jar")
DEOBF_CHANNEL_ID = 1468848637802188872
MAX_FILE_SIZE    = 4_000_000        # 4MB

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[Deobf Bot] Logged in as {bot.user} ({bot.user.id})")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != DEOBF_CHANNEL_ID:
        return
    if not message.attachments:
        return

    att = next(
        (a for a in message.attachments if a.filename.endswith((".lua", ".txt"))),
        None,
    )
    if not att:
        return

    if att.size > MAX_FILE_SIZE:
        await message.reply(f"{message.author.mention} file too large (max 4MB).", mention_author=False)
        return

    await _run_deobf(message, att)


async def _run_deobf(message: discord.Message, att: discord.Attachment):
    loading_emoji = bot.get_emoji(LOADING_EMOJI_ID)
    if loading_emoji:
        try:
            await message.add_reaction(loading_emoji)
        except Exception:
            pass

    async def remove_loading():
        if loading_emoji:
            try:
                await message.remove_reaction(loading_emoji, bot.user)
            except Exception:
                pass

    # Patch runtimeconfig to allow .NET 10
    runtimeconfig_path = pathlib.Path("./out/MoonsecDeobfuscator.runtimeconfig.json")
    try:
        cfg = json.loads(runtimeconfig_path.read_text())
        cfg["runtimeOptions"]["framework"]["version"] = "10.0.0"
        cfg["runtimeOptions"].pop("rollForward", None)
        cfg["runtimeOptions"]["rollForward"] = "Major"
        runtimeconfig_path.write_text(json.dumps(cfg, indent=2))
    except Exception as patch_err:
        print(f"[Deobf] runtimeconfig patch failed: {patch_err}")

    try:
        # Download attachment
        async with aiohttp.ClientSession() as session:
            async with session.get(att.url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    await remove_loading()
                    await message.reply(f"{message.author.mention} failed to download attachment.", mention_author=False)
                    return
                script_bytes = await r.read()

        # Write input to temp file
        with tempfile.NamedTemporaryFile(suffix=".lua", delete=False) as inf:
            inf.write(script_bytes)
            in_path = inf.name

        bytecode_path = in_path + ".bytecode"
        final_path    = in_path + ".final.lua"

        # ── Step 1: Moonsec devirtualize → bytecode ───────────────────────
        try:
            proc = await asyncio.create_subprocess_exec(
                DOTNET_PATH, MOONSEC_DLL, "-dev", "-i", in_path, "-o", bytecode_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                await remove_loading()
                await message.reply(f"{message.author.mention} devirtualization timed out.", mention_author=False)
                return
        finally:
            try: os.unlink(in_path)
            except Exception: pass

        if proc.returncode != 0 or not os.path.exists(bytecode_path):
            await remove_loading()
            err = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
            await message.reply(
                f"{message.author.mention} devirtualization failed:\n```\n{err[:1800]}\n```",
                mention_author=False,
            )
            return

        # ── Step 2: unluac decompile bytecode → readable Lua ─────────────
        unluac_available = os.path.exists(UNLUAC_JAR)
        print(f"[Deobf] unluac.jar exists: {unluac_available} | cwd: {os.getcwd()} | path: {os.path.abspath(UNLUAC_JAR)}")
        if unluac_available:
            try:
                proc2 = await asyncio.create_subprocess_exec(
                    "java", "-jar", UNLUAC_JAR, bytecode_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout2, stderr2 = await asyncio.wait_for(proc2.communicate(), timeout=30)
                except asyncio.TimeoutError:
                    proc2.kill()
                    unluac_available = False
                    print("[Deobf] unluac timed out, falling back to bytecode")

                if unluac_available and proc2.returncode == 0 and stdout2.strip():
                    with open(final_path, "wb") as f:
                        f.write(stdout2)
                else:
                    unluac_available = False
                    print(f"[Deobf] unluac failed: {stderr2.decode(errors='replace')[:200]}")
            except Exception as e:
                unluac_available = False
                print(f"[Deobf] unluac error: {e}")

        # Use unluac output if succeeded, otherwise fall back to raw bytecode
        result_path = final_path if (unluac_available and os.path.exists(final_path)) else bytecode_path
        step_label  = "decompiled" if (unluac_available and os.path.exists(final_path)) else "devirtualized (install unluac.jar for full decompile)"

        with open(result_path, "rb") as f:
            result = f.read()

        for p in [bytecode_path, final_path]:
            try: os.unlink(p)
            except Exception: pass

        await remove_loading()

        out_filename = f"deobf_{att.filename}" if att.filename.endswith(".lua") else f"deobf_{os.path.splitext(att.filename)[0]}.lua"
        await message.reply(
            f"{message.author.mention} here you go!",
            file=discord.File(io.BytesIO(result), filename=out_filename),
            mention_author=False,
        )

    except FileNotFoundError:
        await remove_loading()
        await message.reply(
            f"{message.author.mention} could not find dotnet or the dll. Make sure `out/MoonsecDeobfuscator.dll` exists and dotnet is installed.",
            mention_author=False,
        )
    except Exception as e:
        await remove_loading()
        print(f"[Deobf Bot] Unexpected error: {e}")
        await message.reply(f"{message.author.mention} unexpected error: `{e}`", mention_author=False)

# ── Run ───────────────────────────────────────────────────────────────────────
bot.run(BOT_TOKEN)
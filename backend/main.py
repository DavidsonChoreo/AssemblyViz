"""
FastAPI backend.

Exposes REST endpoints for two ISAs:
  - HYMN  (/api/hymn/assemble, /api/hymn/step)  — 8-bit accumulator machine
  - RISC-V (/api/riscv/assemble, /api/riscv/step) — 32-bit RV32I subset

Both assembler endpoints parse source, assemble to machine words, and return an
initial register/memory snapshot.  

CORS origins are controlled via the ALLOWED_ORIGINS environment variable
(default: http://localhost:5173).
"""

import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hymn.parser import Parser as HymnParser
from hymn.machine import MachineState
from riscv.parser import Parser as RiscvParser
from riscv.assembler import Assembler as RiscvAssembler
from riscv.simulation import Simulation
from riscv.isa import DIRECTIVES

app = FastAPI()

_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The simulator is CPU-bound and every endpoint accepts user-submitted input, so
# the cheapest way to burn server time is a very large body. Source length is
# capped per-field below, but a limit here rejects an oversized request before
# anything parses it — including the HYMN memory array, which the handler
# validates only after Pydantic has already materialised the whole list.
MAX_BODY_BYTES = 256 * 1024


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    length = request.headers.get("content-length")
    if length is not None and int(length) > MAX_BODY_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": f"Request body exceeds {MAX_BODY_BYTES} bytes"},
        )
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok"}

# ── Request models that define what JSON must send ────────────────────────────

# Source is capped here rather than left to the body-size limit alone so the
# caller gets a specific 422 naming the field, instead of a bare 413. 64 KiB is
# far beyond any plausible teaching example; HYMN addresses 32 bytes of memory
# and RISC-V programs here are tens of lines.
MAX_SOURCE_CHARS = 64 * 1024


class HymnAssembleRequest(BaseModel):
    source: str = Field(max_length=MAX_SOURCE_CHARS)

class HymnStepRequest(BaseModel):
    # Exactly 32: the HYMN machine has MEMORY_SIZE = 32 bytes. hymn_step also
    # checks this, but doing it in the model rejects the request during
    # validation rather than after the list has been built.
    memory: list[int] = Field(min_length=32, max_length=32)
    pc: int
    ac: int
    io_input: int = 0   # value supplied by READ pseudo-op

class RiscvAssembleRequest(BaseModel):
    source: str = Field(max_length=MAX_SOURCE_CHARS)

class RiscvStepRequest(BaseModel):
    source: str = Field(max_length=MAX_SOURCE_CHARS)
    step_count: int  # number of machine words to execute

# ── HYMN helpers ──────────────────────────────────────────────────────────────

_HYMN_MNEMONICS = ["HALT", "JUMP", "JZER", "JPOS", "LOAD", "STOR", "ADD", "SUB"]

def _decode_hymn_word(word: int) -> str:
    """Decode a raw 8-bit word into a human-readable mnemonic like "ADD 5" or "READ"."""
    opcode  = (word >> 5) & 0b111
    address = word & 0b11111
    if opcode == 0:
        return "HALT"
    if opcode == 0b100 and address == 30:   # LOAD 30 = READ pseudo-op
        return "READ"
    if opcode == 0b101 and address == 31:   # STOR 31 = WRITE pseudo-op
        return "WRITE"
    return f"{_HYMN_MNEMONICS[opcode]} {address}"

def _hymn_memory_slots(memory: list[int]) -> list[dict]:
    """Build a display array with address, raw value, and decoded instruction for each memory slot."""
    return [
        {
            "address": bin(i)[2:].zfill(5),
            "value":   memory[i],
            "decoded": _decode_hymn_word(memory[i]),
        }
        for i in range(len(memory))
    ]

def _hymn_instruction_lines(source: str) -> list[str]:
    """Strip comments and labels from source, returning bare instruction tokens for the results panel."""
    result = []
    for raw in source.splitlines():
        stripped = raw.split(';')[0].split('#')[0].strip().upper()
        if not stripped:
            continue
        tokens = stripped.split()
        if tokens[0].endswith(':'):
            tokens = tokens[1:]
        if not tokens:
            continue
        result.append(' '.join(tokens))
    return result

# ── RISC-V helpers ────────────────────────────────────────────────────────────

_DATA_DIRECTIVES = DIRECTIVES - {".text", ".data"}

def _riscv_build_display(parsed_lines, symbol_table) -> list[dict]:
    """Build one display row per emitted machine word.

    Pseudo-instructions that expand to multiple words have their continuation
    rows marked with a "↳" symbol.
    """
    assembler = RiscvAssembler()
    assembler._labels = symbol_table
    assembler._pc = 0
    rows = []
    for pl in parsed_lines:
        if pl.mnemonic in _DATA_DIRECTIVES:
            for dw in (pl.data_words or []):
                rows.append({
                    "address":     f"0x{assembler._pc:08x}",
                    "code":        f"0x{dw:08x}",
                    "instruction": pl.mnemonic,
                })
                assembler._pc += 4
        else:
            src_text = f"{pl.mnemonic} {' '.join(pl.operands)}"
            emitted = assembler._assemble_pseudo([pl.mnemonic] + pl.operands)
            for j, w in enumerate(emitted):
                rows.append({
                    "address":     f"0x{assembler._pc + j * 4:08x}",
                    "code":        f"0x{w:08x}",
                    "instruction": src_text if j == 0 else "↳",
                })
            assembler._pc += len(emitted) * 4
    return rows


# ── HYMN endpoints ────────────────────────────────────────────────────────────

@app.post("/api/hymn/assemble")
def hymn_assemble(req: HymnAssembleRequest):
    """Parse and assemble HYMN source, returning instructions, memory display, and initial registers."""
    parser = HymnParser()
    words = parser.parse(req.source)
    if words is None:
        errors = [{"line": e.line_number, "message": e.message} for e in parser.errors]
        raise HTTPException(status_code=400, detail={"errors": errors})

    instr_lines = _hymn_instruction_lines(req.source)
    assembled = [
        {
            "address":     bin(i)[2:].zfill(5),
            "code":        bin(w)[2:].zfill(8),
            "instruction": instr_lines[i] if i < len(instr_lines) else "",
        }
        for i, w in enumerate(words)
    ]

    machine = MachineState(input_fn=lambda: 0, output_fn=lambda v: None)
    try:
        machine.load_program(words)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    snap = machine.snapshot()

    return {
        "words":        words,
        "instructions": assembled,
        "memory":       _hymn_memory_slots(snap["memory"]),
        "registers":    {"pc": snap["pc"], "ac": snap["ac"], "ir": snap["ir"], "zero_flag": snap["zero_flag"], "positive_flag": snap["positive_flag"], "halted": snap["halted"]},
    }

@app.post("/api/hymn/step")
def hymn_step(req: HymnStepRequest):
    """Execute one HYMN instruction.

    Reconstructs machine state from the memory/PC/AC sent by the frontend
    (the backend is stateless), runs one step, and returns updated registers,
    memory, halt status, and any console output.
    """
    machine = MachineState(input_fn=lambda: req.io_input, output_fn=lambda v: None)
    if len(req.memory) != 32:
        raise HTTPException(status_code=400, detail="Memory must be exactly 32 bytes")
    try:
        for i, val in enumerate(req.memory):
            machine.write_memory(i, val)
        machine.pc = req.pc
        machine.ac = req.ac
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        machine.step()
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    snap = machine.snapshot()
    return {
        "pc":            snap["pc"],
        "ac":            snap["ac"],
        "ir":            snap["ir"],
        "zero_flag":     snap["zero_flag"],
        "positive_flag": snap["positive_flag"],
        "halted":        snap["halted"],
        "memory":        _hymn_memory_slots(snap["memory"]),
        "io_output":     snap["io_output"],
    }

# ── RISC-V endpoints ──────────────────────────────────────────────────────────

@app.post("/api/riscv/assemble")
def riscv_assemble(req: RiscvAssembleRequest):
    """Parse and assemble RISC-V source, returning machine words, instruction display rows, and initial registers."""
    rparser = RiscvParser()
    parsed_lines = rparser.parse(req.source)
    if parsed_lines is None:
        errors = [{"line": e.line_number, "message": e.message} for e in rparser.errors]
        raise HTTPException(status_code=400, detail={"errors": errors})

    assembler = RiscvAssembler()
    try:
        words = assembler.assemble(parsed_lines, rparser.symbol_table)
        display_lines = _riscv_build_display(parsed_lines, rparser.symbol_table)
        sim = Simulation()
        sim.load(req.source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if sim._errors:
        errors = [{"line": e.line_number, "message": e.message} for e in sim._errors]
        raise HTTPException(status_code=400, detail={"errors": errors})

    snap = sim.snapshot()
    return {
        "words":        words,
        "instructions": display_lines,
        "registers":    snap["registers"],
        "halted":       snap["halted"],
    }

@app.post("/api/riscv/step")
def riscv_step(req: RiscvStepRequest):
    """Execute RISC-V source up to step N (stateless).

    Re-simulates from scratch each call: runs N-1 warm-up steps silently,
    then captures PC, registers, halt status, and console output from step N only.
    """
    if not (0 <= req.step_count <= 100_000):
        raise HTTPException(status_code=400, detail="step_count must be between 0 and 100000")
    sim = Simulation()
    try:
        sim.load(req.source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if sim._errors:
        errors = [{"line": e.line_number, "message": e.message} for e in sim._errors]
        raise HTTPException(status_code=400, detail={"errors": errors})

    try:
        # Run warm-up steps (N), then capture only the output from the (N+1)th step
        for _ in range(req.step_count):
            if sim.halted:
                break
            sim.step()

        prev_output_len = len(sim._io_output)
        executed_pc = sim.PC          # PC of instruction about to run
        if not sim.halted:
            sim.step()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    snap = sim.snapshot()
    return {
        "PC":          snap["PC"],
        "executed_pc": executed_pc,
        "halted":      snap["halted"],
        "registers":   snap["registers"],
        "io_output":   snap["io_output"][prev_output_len:],
    }


# ── Static SPA ────────────────────────────────────────────────────────────────
# Mounted LAST so the /api routes above win. html=True serves index.html for
# unknown paths, which a client-side router needs.
#
# Absent in local development, where the SPA is served by `npm run dev` and Vite
# proxies /api here — so this mount is skipped rather than failing to start.
_STATIC_DIR = os.getenv(
    "STATIC_DIR",
    os.path.join(os.path.dirname(__file__), "..", "frontend", "dist"),
)
if os.path.isdir(_STATIC_DIR):
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="spa")

# Field engine (archive slot 2292, decompressed to 0x8002f824): replacement for the
# per-tile uv + packet copy block 0x8004e454-0x8004e54c (63 words) of the portrait
# renderer 0x8004e334 (dialogue portraits; this path always mirrors: x1 = x0 - w).
#
# Live on entry: $6 POLY_FT4 template (scratchpad; uv +0xc/+0x14/+0x1c/+0x24, CLUT
# +0xe, tpage +0x16), $12 tile record (+0 CLUT, +2 u, +3 v), $14 layout header
# (+4 w, +6 h), $7 = 1 for the fixed CLUT 0x7f40. Free: $1-$5, $8-$10.
# Live after: $6 $7 $11 $12 $13 $14 $15-$19 $24 $25 (falls through to 0x8004e550).
#
# Original: u0 = u - 1 (mirrored sampling), and u = 0 is clamped to u0 = 0 with the
# width cut by one: those tiles map 15 texels over 16 pixels (a 1-texel seam with
# upscaled rendering or texture packs; at 1x the sampled texels are the same).
# New: on an 8bpp page with x base > 0 and w <= 0x80, u = 0 gives u0 = 0x7f and the
# copied packet's tpage x base - 1 (128 texels to the left): the same texel span as
# every other tile. Otherwise the original clamp is kept.
# Assemble: llvm-mc -triple=mipsel -mcpu=mips1 -filetype=obj portrait_tile.s
.set noreorder
.set noat
.set nomacro
.org 0
    lbu   $4, 2($12)          # u
    lh    $5, 4($14)          # w
    lh    $8, 6($14)          # h
    lbu   $9, 3($12)          # v
    or    $1, $zero, $zero    # 1: packet tpage - 1
    bnez  $4, LU              # tests u before the delay slot decrements it
    addiu $4, $4, -1
    lhu   $2, 0x16($6)        # template tpage
    nop
    andi  $3, $2, 0x180
    xori  $3, $3, 0x80        # 8bpp?
    bnez  $3, LCL
    andi  $3, $2, 0xf         # x base
    beqz  $3, LCL
    sltiu $3, $5, 0x81
    beqz  $3, LCL
    nop
    addiu $4, $zero, 0x7f
    b     LU
    addiu $1, $zero, 1
LCL:
    or    $4, $zero, $zero
    addiu $5, $5, -1
LU:
    addu  $2, $4, $5
    slti  $2, $2, 0x100
    bnez  $2, L1
    addu  $2, $9, $8
    addiu $5, $5, -1
L1:
    slti  $2, $2, 0x100
    bnez  $2, L2
    nop
    addiu $8, $8, -1
L2:
    addu  $2, $4, $5          # u1
    addu  $3, $9, $8          # v1
    sb    $4, 0xc($6)
    sb    $9, 0xd($6)
    sb    $2, 0x14($6)
    sb    $9, 0x15($6)
    sb    $4, 0x1c($6)
    sb    $3, 0x1d($6)
    sb    $2, 0x24($6)
    sb    $3, 0x25($6)
    bnez  $7, LCLUT
    addiu $2, $zero, 0x7f40
    lhu   $2, 0($12)
    nop
LCLUT:
    sh    $2, 0xe($6)
    lui   $9, 0x8008
    lw    $9, -0xf20($9)      # packet pointer *(0x8007f0e0)
    or    $8, $6, $zero
    addiu $10, $6, 0x28
LC:
    lw    $2, 0($8)
    addiu $8, $8, 4
    sw    $2, 0($9)
    bne   $8, $10, LC
    addiu $9, $9, 4
    beqz  $1, LEND
    lhu   $2, -0x12($9)       # packet tpage (packet + 0x16)
    nop
    addiu $2, $2, -1
    sh    $2, -0x12($9)
LEND:

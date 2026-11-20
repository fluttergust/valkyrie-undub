/* Ending-FMV subtitle routine (fmvsub.S, from vpsub): runs right after the FMV player item of pack 4616 (0x800BEA00 + 0x133DC). */
ENTRY(hook)
LoadImage = 0x80021058;
StoreImage = 0x800210B8;
DrawSync = 0x80020DAC;
SECTIONS
{
    . = 0x800D1DDC;
    .text : SUBALIGN(4) { *(.text.hook) *(.text*) }
    .rodata : SUBALIGN(2) { *(.rodata*) }
    fmvend = .;
    /DISCARD/ : { *(.data*) *(.bss*) *(.comment) *(.MIPS.*) *(.reginfo) *(.pdr) *(.gnu.attributes) *(.note*) }
}

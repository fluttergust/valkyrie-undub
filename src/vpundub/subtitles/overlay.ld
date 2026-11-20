/* vpsub overlay: linked to run right after the world-map module (slot 4796, 0x8002F824 + 0x258C0). */
ENTRY(hook)
SECTIONS
{
    . = 0x800550E4;
    .text : { *(.text.hook) *(.text*) }
    .rodata : { *(.rodata*) }
    .data : { *(.data*) *(.got) *(.sdata*) *(.sbss*) *(.bss*) *(COMMON) LONG(0) }
    . = ALIGN(4);
    subdata = .;
    /DISCARD/ : { *(.comment) *(.MIPS.*) *(.reginfo) *(.pdr) *(.gnu.attributes) *(.note*) }
}

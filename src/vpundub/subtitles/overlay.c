/*
 * vpsub runtime: subtitles for the Spiritual Concentration visions, drawn the way the field engine draws
 * the Weeping Lily Meadow vision text (see TECHNICAL.md "Vision text fade" and "In-game patch").
 *
 * Appended to the world-map module (archive slot 4796, loaded at 0x8002F824, ends at 0x800550E4) and
 * entered from the main loop instead of the vision step function (0x80033FE8): ``hook`` calls the step,
 * follows the clip player state and appends GPU packets to the frontmost ordering-table entry:
 *
 *   1. a throwaway 2x1 upload + NOP (emulator quirk, see draw()), then upload the two CLUTs (960,464) and
 *      the glyph cells of the current page (960,256)..(1023,463), a VRAM area no vision uses (GP0 A0,
 *      inline data), then flush the texture cache (GP0 01);
 *   2. per glyph, last character first: E1 (blend B-F) + sprite with the all-white CLUT, then E1 (B+F) +
 *      sprite with the ink/outline CLUT, both modulated by m (0..0x80).
 *
 * A page may outlive its clip (clips start when the game decides): it is *carried* through the next clips
 * until its stop, unless a page of the new clip wants the screen; then the carried page fades out at
 * CUT_FADE per vsync and the new one is drawn once it is gone (one page's cells in VRAM at a time).
 *
 * Everything is re-sent every frame, so nothing depends on VRAM surviving between frames. The area is
 * saved to RAM (StoreImage) before the first page of a vision and restored (LoadImage) once the vision id
 * is back to 0 and nothing is drawn: the world map keeps data there (its dialogue glyph cache, around
 * vision 1). A page still showing when the vision ends finishes its fade-out first (the vision picture
 * stays up for seconds after the last voice; the world map leaves the area alone for 590+ frames). Timing is in
 * vsyncs (PsyQ VSync counter) since the clip started (+0x265 0 -> 1). The Python reference model of the
 * same rules is pages.py (modulation(), active(), Runtime).
 *
 * Data (``subdata``, appended after the code by patch.py, little endian, offsets from subdata):
 *   u32 vis_off[27]                       per vision id (0 = no subtitles)
 *   u32 cells_off, u32 ncells             cell bitmaps: ncells x 56 u16 (4 hw x 14 rows, 4bpp)
 *   vision: u32 nclips, u32 clip_off[nclips] (0 = none)
 *   clip:   u32 npages, then pages back to back
 *   page:   u16 start, end, stop; u8 speed, fade; u8 nglyph, ncell; u16 0;
 *           u8 cell[ncell] (padded to 2); glyph[nglyph] {u16 x; u8 y, slot, order, w} (padded to 4)
 */
typedef unsigned char u8;
typedef signed char s8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef short s16;

struct rect { s16 x, y, w, h; };

#define STATE    (*(u8 *volatile *)0x8004F17C)
#define OTBASE   (*(u8 *volatile *)0x8004F148)
#define PKTBASE  (*(u8 *volatile *)0x8004F14C)
#define PKTOFF   (*(volatile u32 *)0x8004F150)
#define VCOUNT   (*(volatile u32 *)0x8002D094)
#define vision_step ((void (*)(void))0x80033FE8)
/* PsyQ libgpu in the exe (found by their debug-print strings): queued like the game's own transfers */
#define LoadImage  ((int (*)(const struct rect *, u32 *))0x80021058)
#define StoreImage ((int (*)(const struct rect *, u32 *))0x800210B8)
#define DrawSync   ((int (*)(int))0x80020DAC)
#define SAVEBUF    ((u32 *)0x80083030)       /* 64 x 211 hw = 0x6980 bytes, up to the free-RAM end 0x800899B0 */

#define OT_FRONT 0x11C1C          /* last of the 0x708 OT entries at buffer + 0x10000: drawn last */
#define PKT_MAX  0x10000          /* packets live below the OT */
#define NVIS     27

#define TEX_X    960              /* glyph cells: 16 per row of 16x16 px in texture page (960,256), free */
#define TEX_Y    256
#define CLUT_X   960
#define CLUT_Y   464              /* + dummy upload row at 466 */
#define CELL_HW  4
#define CELL_H   14
#define E1_SUB   0xE100025Fu      /* page 15/1, blend 2 (B-F), 4bpp, dither (field text: page 13) */
#define E1_ADD   0xE100023Fu      /* blend 1 (B+F) */
#define FULL     0x80
#define FADE_IN  4
#define CUT_FADE 8                /* carried page cut by a new one */
#define NODE_MAX 16               /* words per linked-list node (Mednafen FIFO, see upload()) */

struct page {
    u16 start, end, stop;
    u8 speed, fade, nglyph, ncell;
    u16 zero;
};

struct glyph {
    u16 x;
    u8 y, slot, order, w;
};

extern const u8 subdata[];

static s8 cur_clip = -1;
static u8 cur_vid;
static u32 t0;
static const struct page *last;  /* page drawn on the previous vsync, its clip start */
static u32 last_t0;
static const struct page *carry; /* page of an earlier clip still showing, its clip start */
static u32 carry_t0, cut;        /* cut: vsync its fast fade began, if cutting */
static u8 cutting;
static u8 saved;                 /* the area's previous contents are in SAVEBUF */
static const struct rect area = { TEX_X, TEX_Y, 64, CLUT_Y + 3 - TEX_Y };   /* cells, CLUTs, dummy row */

static const u16 clut[32] = {
    0, 0xFFFF, 0xFFFF, 0xFFFF, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,     /* subtract: all texels */
    0, 0x8C63, 0xDEF7, 0x8C63, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,     /* add: outline 24, ink 184 */
};

static inline u32 rd32(const u8 *p) { return *(const u32 *)p; }

static u32 *link;                /* tag word of the last packet emitted */

static u32 *emit(u32 *p, u32 n)
{
    *p = n << 24;
    if (link)
        *link = (*link & 0xFF000000u) | ((u32)p & 0xFFFFFFu);
    link = p;
    return p + 1;
}

/* CPU->VRAM upload (GP0 A0 + inline data), split into linked-list nodes of at most NODE_MAX words: the GPU
 * sees one continuous command, but Mednafen (BizHawk's Nymashock core) only checks for GPU FIFO room at the start of a node
 * and drops the words that overflow its 16-word FIFO inside one (a 31-word cell upload lost its middle while
 * the GPU was still busy with the game's primitives). Real hardware paces every word; nodes are free there. */
static u32 *upload(u32 *p, u32 x, u32 y, u32 w, u32 h, const u16 *src)
{
    u32 left = 3 + w * h / 2, k, i;
    u32 words[3] = { 0xA0000000u, y << 16 | x, h << 16 | w };

    for (i = 0; left; ) {
        k = left < NODE_MAX ? left : NODE_MAX;
        p = emit(p, k);
        left -= k;
        for (; k; k--, i++) {
            if (i < 3)
                *p++ = words[i];
            else {
                *p++ = src[0] | (u32)src[1] << 16;
                src += 2;
            }
        }
    }
    return p;
}

/* words (data + node tags) an upload of n data words takes */
#define UPLOAD_WORDS(n) ((3 + (n)) + (3 + (n) + NODE_MAX - 1) / NODE_MAX)

static u32 *sprite(u32 *p, u32 e1, u32 m, const struct glyph *g, u32 clut_id)
{
    u32 u = (g->slot & 15) * 16, v = (g->slot >> 4) * 16;
    p = emit(p, 1);
    *p++ = e1;
    p = emit(p, 4);
    *p++ = 0x66000000u | m << 16 | m << 8 | m;
    *p++ = (u32)g->y << 16 | g->x;
    *p++ = clut_id << 16 | v << 8 | u;
    *p++ = CELL_H << 16 | g->w;
    return p;
}

static int modulation(const struct page *pg, const struct glyph *g, int t, int cap)
{
    int m = (t - pg->start - g->order * pg->speed + 1) * FADE_IN;
    if (m > cap)
        m = cap;
    if (t >= pg->end) {
        int out = FULL - (t - pg->end + 1) * pg->fade;
        if (out < m)
            m = out;
    }
    return m < 0 ? 0 : m;
}

static void draw(const struct page *pg, int t, int cap)
{
    const u8 *cells = (const u8 *)(pg + 1);
    const struct glyph *gl = (const struct glyph *)(cells + ((pg->ncell + 1) & ~1));
    const u16 *bank = (const u16 *)(subdata + rd32(subdata + 4 * NVIS));
    u32 need = 6 + UPLOAD_WORDS(16) + pg->ncell * UPLOAD_WORDS(CELL_HW * CELL_H / 2) + 2 + pg->nglyph * 14;
    u32 off = PKTOFF, *p, *first, *ot;
    int i;

    if (off + need * 4 > PKT_MAX)
        return;
    first = p = (u32 *)(PKTBASE + off);
    link = 0;
    /* A throwaway 2x1 upload + NOP first: on BizHawk (Nymashock) the first transfer of the list used to lose
     * or gain a word (likely the same FIFO overflow as in upload(); kept as cheap insurance, a NOP on hardware). */
    p = emit(p, 5);
    *p++ = 0xA0000000u;
    *p++ = (CLUT_Y + 2) << 16 | CLUT_X;
    *p++ = 1 << 16 | 2;
    *p++ = 0;
    *p++ = 0;
    p = upload(p, CLUT_X, CLUT_Y, 16, 2, clut);
    for (i = 0; i < pg->ncell; i++)
        p = upload(p, TEX_X + (i & 15) * CELL_HW, TEX_Y + (i >> 4) * 16, CELL_HW, CELL_H,
                   bank + cells[i] * (CELL_HW * CELL_H));
    p = emit(p, 1);
    *p++ = 0x01000000u;                                 /* flush the texture cache */
    for (i = pg->nglyph - 1; i >= 0; i--) {             /* ordering-table order: last character first */
        int m = modulation(pg, &gl[i], t, cap);
        if (!m)
            continue;
        p = sprite(p, E1_SUB, m, &gl[i], CLUT_Y << 6 | CLUT_X >> 4);
        p = sprite(p, E1_ADD, m, &gl[i], (CLUT_Y + 1) << 6 | CLUT_X >> 4);
    }
    ot = (u32 *)(OTBASE + OT_FRONT);
    *link = (*link & 0xFF000000u) | (*ot & 0xFFFFFFu);
    *ot = (*ot & 0xFF000000u) | ((u32)first & 0xFFFFFFu);
    PKTOFF = (u8 *)p - PKTBASE;
}

static const struct page *find(int vid, int clip, int t)
{
    u32 vo = rd32(subdata + 4 * vid), co, n;
    const u8 *c;
    if (!vo || clip >= (int)rd32(subdata + vo))
        return 0;
    co = rd32(subdata + vo + 4 + 4 * clip);
    if (!co)
        return 0;
    c = subdata + co;
    n = rd32(c);
    c += 4;
    while (n--) {
        const struct page *pg = (const struct page *)c;
        if (t >= pg->start && t < pg->stop)
            return pg;
        c += sizeof *pg + ((pg->ncell + 1) & ~1) + pg->nglyph * sizeof(struct glyph);
        c = (const u8 *)(((u32)c + 3) & ~3u);
    }
    return 0;
}

static void keep_area(void)
{
    if (!saved) {                                       /* first page of this vision: keep the area */
        StoreImage(&area, SAVEBUF);
        DrawSync(0);
        saved = 1;
    }
}

/* Draw the carried page if it is still showing (cut_now: a new page wants the screen). */
static int draw_carry(u32 now, int cut_now)
{
    u32 ct = now - carry_t0;
    int cap = FULL;

    if (ct >= carry->stop)
        return 0;
    if (cut_now && !cutting) {
        cutting = 1;
        cut = now;
    }
    if (cutting)
        cap = FULL - (int)(now - cut + 1) * CUT_FADE;
    if (cap <= 0)
        return 0;
    keep_area();
    draw(carry, ct, cap);
    return 1;
}

void hook(void)
{
    u8 *s;
    int vid, sub, clip;
    const struct page *pg = 0;
    u32 now;

    vision_step();
    s = STATE;
    if ((u32)s < 0x80000000u || (u32)s >= 0x80200000u)
        return;
    vid = s[0x260];
    sub = (s8)s[0x265];
    clip = s[0x264];
    now = VCOUNT;
    if (vid != cur_vid) {                               /* vision over or another one */
        if (!carry && last && now - last_t0 < last->stop) {
            carry = last;                               /* a page still showing finishes its fade-out */
            carry_t0 = last_t0;
            cutting = 0;
        }
        last = 0;
        cur_clip = -1;
        cur_vid = vid;
        if (vid && vid < NVIS)                          /* another vision: start clean */
            carry = 0;
    }
    if (vid == 0 || vid >= NVIS) {                      /* the vision picture is still up for seconds */
        if (carry && draw_carry(now, 0))
            return;
        carry = 0;
        if (saved) {                                    /* the world map owns VRAM again: give back what */
            LoadImage(&area, SAVEBUF);                  /* was there (e.g. its text cache) */
            saved = 0;
        }
        return;
    }
    if (sub == 1 && clip != cur_clip) {                 /* clip playback just started */
        if (!carry && last && now - last_t0 < last->stop) {
            carry = last;
            carry_t0 = last_t0;
            cutting = 0;
        }
        last = 0;
        cur_clip = clip;
        t0 = now;
    }
    if (cur_clip >= 0)
        pg = find(vid, cur_clip, now - t0);
    if (carry) {
        if (draw_carry(now, pg != 0))
            return;
        carry = 0;
    }
    if (!pg)
        return;
    keep_area();
    draw(pg, now - t0, FULL);
    last = pg;
    last_t0 = t0;
}

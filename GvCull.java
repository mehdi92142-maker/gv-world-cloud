import java.io.*;
import java.nio.file.*;
import java.util.*;
import java.util.zip.*;

/**
 * GvCull -- copy of a world where every empty cell (air / water / plants) that can NOT be seen from outside is filled with
 * stone, so BlueMap (and everything after it) never meshes, stores or loads it. The original world is not touched.
 *
 *   java GvCull <world in> <world out> X0 Z0 X1 Z1 [--halo 64] [--margin 3]
 *
 * "Seen from outside" (user rule 2026-10-14): straight rays from open sky (cells above the highest opaque block of their
 * column) in 98 directions (components -2..2, the half-way cell of a 2-cell step must be free too) through see-through
 * cells; then a margin of 3 steps (6-neighbour) through see-through cells for in-between angles. Caves: the part visible
 * from the mouth is kept, the second bend is not; deep vertical shafts down to the bottom. Sealed caves vanish.
 * Each direction is ONE sweep (cells in an order where the predecessor along the ray comes first).
 * Works per region (512x512) with a halo, so RAM is fixed whatever the world size (2 x ~160 MB byte arrays).
 */
public class GvCull {
    static final int H = 384, Y0 = -64;

    // ---------------------------------------------------------------- minimal NBT (read + write, order preserved)
    static Object readTag(DataInputStream in, int t) throws IOException {
        switch (t) {
            case 1: return in.readByte();
            case 2: return in.readShort();
            case 3: return in.readInt();
            case 4: return in.readLong();
            case 5: return in.readFloat();
            case 6: return in.readDouble();
            case 7: { byte[] b = new byte[in.readInt()]; in.readFully(b); return b; }
            case 8: return in.readUTF();
            case 9: { int et = in.readByte(), n = in.readInt(); NList l = new NList(); l.et = et;
                for (int i = 0; i < n; i++) l.add(readTag(in, et)); return l; }
            case 10: { LinkedHashMap<String, Object> m = new LinkedHashMap<>(); NComp c = new NComp(); c.m = m;
                while (true) { int tt = in.readByte(); if (tt == 0) break; String k = in.readUTF(); m.put(k, readTag(in, tt)); c.t.put(k, tt); }
                return c; }
            case 11: { int[] a = new int[in.readInt()]; for (int i = 0; i < a.length; i++) a[i] = in.readInt(); return a; }
            case 12: { long[] a = new long[in.readInt()]; for (int i = 0; i < a.length; i++) a[i] = in.readLong(); return a; }
        }
        throw new IOException("tag " + t);
    }
    static void writeTag(DataOutputStream o, int t, Object v) throws IOException {
        switch (t) {
            case 1: o.writeByte((Byte) v); break;
            case 2: o.writeShort((Short) v); break;
            case 3: o.writeInt((Integer) v); break;
            case 4: o.writeLong((Long) v); break;
            case 5: o.writeFloat((Float) v); break;
            case 6: o.writeDouble((Double) v); break;
            case 7: { byte[] b = (byte[]) v; o.writeInt(b.length); o.write(b); break; }
            case 8: o.writeUTF((String) v); break;
            case 9: { NList l = (NList) v; o.writeByte(l.isEmpty() ? 0 : l.et); o.writeInt(l.size()); for (Object e : l) writeTag(o, l.et, e); break; }
            case 10: { NComp c = (NComp) v; for (Map.Entry<String, Object> e : c.m.entrySet()) { int tt = c.t.get(e.getKey());
                o.writeByte(tt); o.writeUTF(e.getKey()); writeTag(o, tt, e.getValue()); } o.writeByte(0); break; }
            case 11: { int[] a = (int[]) v; o.writeInt(a.length); for (int x : a) o.writeInt(x); break; }
            case 12: { long[] a = (long[]) v; o.writeInt(a.length); for (long x : a) o.writeLong(x); break; }
            default: throw new IOException("tag " + t);
        }
    }
    static class NList extends ArrayList<Object> { int et; }
    static class NComp { LinkedHashMap<String, Object> m; HashMap<String, Integer> t = new HashMap<>(); Object get(String k) { return m.get(k); } }

    // ---------------------------------------------------------------- region files
    static NComp[] readRegion(Path p) throws IOException {
        NComp[] out = new NComp[1024];
        if (!Files.exists(p)) return out;
        byte[] buf = Files.readAllBytes(p);
        for (int i = 0; i < 1024; i++) {
            int off = ((buf[i * 4] & 255) << 16 | (buf[i * 4 + 1] & 255) << 8 | (buf[i * 4 + 2] & 255)) * 4096;
            if (off == 0) continue;
            int len = (buf[off] & 255) << 24 | (buf[off + 1] & 255) << 16 | (buf[off + 2] & 255) << 8 | (buf[off + 3] & 255);
            int comp = buf[off + 4];
            InputStream is = new ByteArrayInputStream(buf, off + 5, len - 1);
            if (comp == 2) is = new InflaterInputStream(is); else if (comp == 1) is = new GZIPInputStream(is); else if (comp != 3) throw new IOException("compression " + comp);
            DataInputStream in = new DataInputStream(new BufferedInputStream(is));
            in.readByte(); in.readUTF();
            out[i] = (NComp) readTag(in, 10);
        }
        return out;
    }
    static void writeRegion(Path p, NComp[] chunks) throws IOException {
        ByteArrayOutputStream body = new ByteArrayOutputStream();
        byte[] head = new byte[8192]; int sector = 2;
        int now = (int) (System.currentTimeMillis() / 1000);
        for (int i = 0; i < 1024; i++) {
            if (chunks[i] == null) continue;
            ByteArrayOutputStream raw = new ByteArrayOutputStream();
            DeflaterOutputStream z = new DeflaterOutputStream(raw, new Deflater(4));
            DataOutputStream o = new DataOutputStream(new BufferedOutputStream(z));
            o.writeByte(10); o.writeUTF(""); writeTag(o, 10, chunks[i]); o.close();
            byte[] d = raw.toByteArray();
            int len = d.length + 1, n = (len + 4 + 4095) / 4096;
            ByteArrayOutputStream rec = new ByteArrayOutputStream(n * 4096);
            DataOutputStream ro = new DataOutputStream(rec); ro.writeInt(len); ro.writeByte(2); ro.write(d);
            while (rec.size() < n * 4096) rec.write(0);
            body.write(rec.toByteArray());
            head[i * 4] = (byte) (sector >> 16); head[i * 4 + 1] = (byte) (sector >> 8); head[i * 4 + 2] = (byte) sector; head[i * 4 + 3] = (byte) n;
            head[4096 + i * 4] = (byte) (now >> 24); head[4096 + i * 4 + 1] = (byte) (now >> 16); head[4096 + i * 4 + 2] = (byte) (now >> 8); head[4096 + i * 4 + 3] = (byte) now;
            sector += n;
        }
        try (OutputStream os = new BufferedOutputStream(Files.newOutputStream(p))) { os.write(head); body.writeTo(os); }
    }

    // ---------------------------------------------------------------- see-through blocks (= tools/bake_buildtime.see_through)
    static final Set<String> EXACT = new HashSet<>(Arrays.asList(("air cave_air void_air water lava bubble_column short_grass tall_grass grass fern " +
        "large_fern seagrass tall_seagrass kelp kelp_plant vine dead_bush sugar_cane bamboo lily_pad cobweb sea_pickle glow_lichen sculk_vein " +
        "pink_petals wildflowers leaf_litter bush firefly_bush sweet_berry_bush cactus cactus_flower pumpkin_stem melon_stem attached_pumpkin_stem " +
        "attached_melon_stem spore_blossom pointed_dripstone chain iron_chain ladder cave_vines cave_vines_plant twisting_vines weeping_vines " +
        "hanging_roots mangrove_roots moss_carpet pale_moss_carpet pale_hanging_moss small_dripleaf big_dripleaf big_dripleaf_stem azalea " +
        "flowering_azalea dandelion poppy blue_orchid allium azure_bluet oxeye_daisy cornflower lily_of_the_valley wither_rose torchflower " +
        "sunflower lilac rose_bush peony pitcher_plant short_dry_grass tall_dry_grass closed_eyeblossom open_eyeblossom brown_mushroom " +
        "red_mushroom crimson_roots warped_roots nether_sprouts frogspawn scaffolding rail powered_rail detector_rail activator_rail " +
        "redstone_wire tripwire lever iron_bars light barrier structure_void fire soul_fire golden_dandelion wheat carrots potatoes " +
        "beetroots torchflower_crop pitcher_crop cocoa nether_wart").split(" ")));
    static final String[] SUFFIX = {"_leaves", "_sapling", "glass", "_glass_pane", "_carpet", "_torch", "_coral", "_coral_fan",
        "_coral_wall_fan", "_tulip", "_fence", "_fence_gate", "_wall", "_door", "_trapdoor", "_sign", "_banner", "_button",
        "_pressure_plate", "_bars", "candle", "_crop", "_vines", "_vines_plant", "_mushroom", "_roots", "_propagule"};
    static boolean seeThrough(String name, NComp props) {
        String b = name.substring(name.indexOf(':') + 1);
        if (b.equals("snow")) { Object l = props == null ? null : props.get("layers"); return l == null || !"8".equals(l); }
        if (EXACT.contains(b)) return true;
        for (String s : SUFFIX) if (b.endsWith(s)) return true;
        return false;
    }

    // ---------------------------------------------------------------- one window = region + halo
    static int W, L;                       // window size in blocks
    static byte[] free;                    // 1 = see-through
    static byte[] vis;                     // 1 = visible from outside (then margin)
    static int idx(int x, int y, int z) { return (y * L + z) * W + x; }

    static void decodeSection(NComp sec, int[] outIdx, boolean[][] palFree) {
        // fills outIdx[4096] with palette indices, palFree[0] = per-palette see-through
        NComp bs = (NComp) sec.get("block_states");
        NList pal = (NList) bs.get("palette");
        boolean[] pf = new boolean[pal.size()];
        for (int i = 0; i < pf.length; i++) { NComp e = (NComp) pal.get(i); pf[i] = seeThrough((String) e.get("Name"), (NComp) e.get("Properties")); }
        palFree[0] = pf;
        long[] data = (long[]) bs.get("data");
        if (data == null) { Arrays.fill(outIdx, 0); return; }
        int bits = Math.max(4, 32 - Integer.numberOfLeadingZeros(pal.size() - 1)), per = 64 / bits; long mask = (1L << bits) - 1;
        for (int i = 0; i < 4096; i++) outIdx[i] = (int) ((data[i / per] >>> ((i % per) * bits)) & mask);
    }

    public static void main(String[] a) throws Exception {
        Path in = Paths.get(a[0]), out = Paths.get(a[1]);
        int X0 = Integer.parseInt(a[2]), Z0 = Integer.parseInt(a[3]), X1 = Integer.parseInt(a[4]), Z1 = Integer.parseInt(a[5]);
        int halo = 64, margin = 3;
        for (int i = 6; i < a.length; i++) { if (a[i].equals("--halo")) halo = Integer.parseInt(a[++i]); if (a[i].equals("--margin")) margin = Integer.parseInt(a[++i]); }
        Path rin = in.resolve("dimensions/minecraft/overworld/region"), rout = out.resolve("dimensions/minecraft/overworld/region");
        if (!Files.isDirectory(rin)) rin = in.resolve("region");
        Files.createDirectories(rout);
        if (Files.exists(in.resolve("level.dat"))) Files.copy(in.resolve("level.dat"), out.resolve("level.dat"), StandardCopyOption.REPLACE_EXISTING);
        W = 512 + 2 * halo; L = 512 + 2 * halo;
        free = new byte[W * L * H]; vis = new byte[W * L * H];
        int[] dirs = new int[98 * 3]; int nd = 0;
        for (int dx = -2; dx <= 2; dx++) for (int dy = -2; dy <= 2; dy++) for (int dz = -2; dz <= 2; dz++) {
            if (dx == 0 && dy == 0 && dz == 0) continue;
            if (gcd(gcd(Math.abs(dx), Math.abs(dy)), Math.abs(dz)) != 1) continue;
            dirs[nd * 3] = dx; dirs[nd * 3 + 1] = dy; dirs[nd * 3 + 2] = dz; nd++;
        }
        long t0 = System.currentTimeMillis(), filledTot = 0, freeTot = 0; byte[] skyBuf = null;
        int[] sidx = new int[4096]; boolean[][] pf = new boolean[1][];
        HashMap<Long, long[]> cache = new HashMap<>();          // per region: see-through bits (512 x 512 x 384), null = missing
        byte[] r = new byte[W * L * H];
        for (int rx = X0 >> 9; rx <= X1 >> 9; rx++) for (int rz = Z0 >> 9; rz <= Z1 >> 9; rz++) {
            long tr = System.currentTimeMillis();
            int wx0 = rx * 512 - halo, wz0 = rz * 512 - halo;
            // 1. see-through grid of the window (outside the world / missing chunks = see-through: terrain unknown -> open)
            Arrays.fill(free, (byte) 1); Arrays.fill(vis, (byte) 0);
            for (int qx = (wx0 >> 9); qx <= ((wx0 + W - 1) >> 9); qx++) for (int qz = (wz0 >> 9); qz <= ((wz0 + L - 1) >> 9); qz++) {
                long key = ((long) qx << 32) ^ (qz & 0xffffffffL);
                if (!cache.containsKey(key)) cache.put(key, regionBits(rin.resolve("r." + qx + "." + qz + ".mca"), sidx, pf));
                long[] bits = cache.get(key); if (bits == null) continue;
                for (int y = 0; y < H; y++) for (int lz = 0; lz < 512; lz++) {
                    int z = qz * 512 + lz - wz0; if (z < 0 || z >= L) continue;
                    for (int lx = 0; lx < 512; lx++) {
                        int x = qx * 512 + lx - wx0; if (x < 0 || x >= W) continue;
                        long b = ((long) y * 512 + lz) * 512 + lx;
                        free[idx(x, y, z)] = (byte) ((bits[(int) (b >>> 6)] >>> (b & 63)) & 1);
                    }
                }
            }
            // 2. open sky
            for (int z = 0; z < L; z++) for (int x = 0; x < W; x++) {
                for (int y = H - 1; y >= 0; y--) { int i = idx(x, y, z); if (free[i] == 0) break; vis[i] = 1; }
            }
            byte[] sky = skyBuf == null ? (skyBuf = new byte[vis.length]) : skyBuf; System.arraycopy(vis, 0, sky, 0, vis.length);
            // 3. 98 straight-ray sweeps
            for (int d = 0; d < nd; d++) {
                int dx = dirs[d * 3], dy = dirs[d * 3 + 1], dz = dirs[d * 3 + 2];
                int hx = dx / 2, hy = dy / 2, hz = dz / 2; boolean two = Math.abs(dx) == 2 || Math.abs(dy) == 2 || Math.abs(dz) == 2;
                int ys = dy > 0 ? 0 : H - 1, ye = dy > 0 ? H : -1, yst = dy > 0 ? 1 : -1;
                int zs = dz > 0 ? 0 : L - 1, ze = dz > 0 ? L : -1, zst = dz > 0 ? 1 : -1;
                int xs = dx > 0 ? 0 : W - 1, xe = dx > 0 ? W : -1, xst = dx > 0 ? 1 : -1;
                // a ray marker per direction: r = cell is on a ray from the sky in this direction
                System.arraycopy(sky, 0, r, 0, r.length);
                for (int y = ys; y != ye; y += yst) {
                    int py = y - dy; if (py < 0 || py >= H) continue;
                    for (int z = zs; z != ze; z += zst) {
                        int pz = z - dz; if (pz < 0 || pz >= L) continue;
                        int rowBase = (y * L + z) * W, prow = (py * L + pz) * W;
                        for (int x = xs; x != xe; x += xst) {
                            int px = x - dx; if (px < 0 || px >= W) continue;
                            int i = rowBase + x;
                            if (r[i] != 0 || free[i] == 0 || r[prow + px] == 0) continue;
                            if (two && free[idx(px + hx, py + hy, pz + hz)] == 0) continue;
                            r[i] = 1;
                        }
                    }
                }
                for (int i = 0; i < r.length; i++) if (r[i] != 0) vis[i] = 1;
            }
            // 4. margin: `margin` steps through see-through cells
            for (int m = 0; m < margin; m++) {
                byte[] nv = r; System.arraycopy(vis, 0, nv, 0, vis.length);
                for (int y = 0; y < H; y++) for (int z = 0; z < L; z++) for (int x = 0; x < W; x++) {
                    int i = idx(x, y, z); if (vis[i] != 0 || free[i] == 0) continue;
                    if ((x > 0 && vis[i - 1] != 0) || (x < W - 1 && vis[i + 1] != 0) || (z > 0 && vis[i - W] != 0) || (z < L - 1 && vis[i + W] != 0)
                        || (y > 0 && vis[i - W * L] != 0) || (y < H - 1 && vis[i + W * L] != 0)) nv[i] = 1;
                }
                System.arraycopy(nv, 0, vis, 0, vis.length);
            }
            // 5. write the region: invisible see-through cells -> stone
            NComp[] reg = readRegion(rin.resolve("r." + rx + "." + rz + ".mca"));
            long filled = 0, fr = 0;
            for (int ci = 0; ci < 1024; ci++) {
                NComp c = reg[ci]; if (c == null) continue;
                int cx = rx * 32 + (ci & 31), cz = rz * 32 + (ci >> 5);
                int bx0 = cx * 16 - wx0, bz0 = cz * 16 - wz0;
                for (Object so : (NList) c.get("sections")) {
                    NComp sec = (NComp) so; if (sec.get("block_states") == null) continue;
                    int sy = (Byte) sec.get("Y") * 16 - Y0; if (sy < 0 || sy >= H) continue;
                    decodeSection(sec, sidx, pf);
                    boolean any = false;
                    for (int i = 0; i < 4096; i++) {
                        if (!pf[0][sidx[i]]) continue;
                        fr++;
                        int x = bx0 + (i & 15), z = bz0 + ((i >> 4) & 15), y = sy + (i >> 8);
                        if (vis[idx(x, y, z)] == 0) { sidx[i] = -1; any = true; filled++; }
                    }
                    if (any) encodeSection(sec, sidx);
                }
                // ticks of filled cells would only re-flow water into stone; harmless, but drop fluid ticks of the chunk
            }
            filledTot += filled; freeTot += fr;
            writeRegion(rout.resolve("r." + rx + "." + rz + ".mca"), reg);
            System.out.printf("region %d,%d: %,d of %,d see-through cells filled (%.0f%%) %.1fs%n", rx, rz, filled, fr, 100.0 * filled / Math.max(1, fr),
                (System.currentTimeMillis() - tr) / 1000.0);
            // keep only the regions the next windows still need
            final int crx = rx, crz = rz;
            cache.keySet().removeIf(k -> (int) (k >> 32) < crx - 1);
        }
        System.out.printf("done: %,d of %,d see-through cells filled (%.0f%%) in %.0fs%n", filledTot, freeTot, 100.0 * filledTot / Math.max(1, freeTot),
            (System.currentTimeMillis() - t0) / 1000.0);
    }

    // all see-through bits of one region file (missing chunk = see-through), NBT dropped right away
    static long[] regionBits(Path p, int[] sidx, boolean[][] pf) throws IOException {
        if (!Files.exists(p)) return null;
        long[] bits = new long[512 * 512 * H / 64];
        Arrays.fill(bits, -1L);
        NComp[] reg = readRegion(p);
        for (int ci = 0; ci < 1024; ci++) {
            NComp c = reg[ci]; if (c == null) continue;
            int bx0 = (ci & 31) * 16, bz0 = (ci >> 5) * 16;
            for (Object so : (NList) c.get("sections")) {
                NComp sec = (NComp) so; if (sec.get("block_states") == null) continue;
                int sy = (Byte) sec.get("Y") * 16 - Y0; if (sy < 0 || sy >= H) continue;
                decodeSection(sec, sidx, pf);
                for (int i = 0; i < 4096; i++) {
                    if (pf[0][sidx[i]]) continue;
                    long b = ((long) (sy + (i >> 8)) * 512 + bz0 + ((i >> 4) & 15)) * 512 + bx0 + (i & 15);
                    bits[(int) (b >>> 6)] &= ~(1L << (b & 63));
                }
            }
        }
        return bits;
    }

    // palette index -1 = stone
    static void encodeSection(NComp sec, int[] sidx) {
        NComp bs = (NComp) sec.get("block_states");
        NList pal = (NList) bs.get("palette");
        int stone = -1;
        for (int i = 0; i < pal.size(); i++) if ("minecraft:stone".equals(((NComp) pal.get(i)).get("Name")) && ((NComp) pal.get(i)).get("Properties") == null) stone = i;
        if (stone < 0) { NComp e = new NComp(); e.m = new LinkedHashMap<>(); e.m.put("Name", "minecraft:stone"); e.t.put("Name", 8); pal.add(e); if (pal.et == 0) pal.et = 10; stone = pal.size() - 1; }
        for (int i = 0; i < 4096; i++) if (sidx[i] < 0) sidx[i] = stone;
        int bits = Math.max(4, 32 - Integer.numberOfLeadingZeros(pal.size() - 1)), per = 64 / bits;
        long[] data = new long[(4096 + per - 1) / per];
        for (int i = 0; i < 4096; i++) data[i / per] |= (long) sidx[i] << ((i % per) * bits);
        bs.m.put("data", data); bs.t.put("data", 12);
    }
    static int gcd(int a, int b) { return b == 0 ? a : gcd(b, a % b); }
}

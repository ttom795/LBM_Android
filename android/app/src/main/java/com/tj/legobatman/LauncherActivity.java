package com.tj.legobatman;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.DocumentsContract;
import android.provider.Settings;
import android.util.TypedValue;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;

/* Start screen, like the Skyrim port's: pick the folder that holds the LEGO
 * Batman install and press Play. The port options are all in the in-game
 * Video and Port Settings menu. Back during play opens this screen over the
 * paused game with just Resume (Back again also resumes). The game gets the folder
 * as argv[1] and <folder>/port/save as argv[2]; the log, last_exit.txt and the
 * saves all live in <folder>/port. */
public class LauncherActivity extends Activity {
    private static final int REQ_FOLDER = 1, REQ_PERM = 2;
    private SharedPreferences prefs;
    private TextView folderText, folderStatus;
    private Button playButton;
    private boolean pendingPick, pendingPlay;  /* continue once the storage prompt is answered */
    private boolean askedAllFiles;
    private LinearLayout folderBox;
    private boolean inGame;

    // SHA-1 of the Steamless-stripped LEGOBatman.exe the port was recompiled
    // from: other builds have different code at different addresses.
    private static final String SHA_EXE = "ed6c37c5b896c0b91d90ebeebfe770c9a9630d32";

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        prefs = getSharedPreferences("launcher", MODE_PRIVATE);
        if (prefs.getString("folder", null) == null) {
            // 0.1.6 and older read the game from the app's own folder: keep using it
            File old = oldGameDir();
            if (old != null && findCi(old, "GAME.DAT") != null) prefs.edit().putString("folder", old.getPath()).apply();
        }
        buildUi();
        refreshFolder();
    }

    @Override
    protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        setIntent(i);
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshFolder();
    }

    private File oldGameDir() {
        File ext = getExternalFilesDir(null);
        return ext == null ? null : new File(ext, "game");
    }

    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }

    private TextView text(LinearLayout parent, String s, int sp, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(Color.rgb(235, 235, 225));
        if (bold) t.setTypeface(null, android.graphics.Typeface.BOLD);
        t.setPadding(0, dp(6), 0, dp(2));
        parent.addView(t);
        return t;
    }

    private void buildUi() {
        ScrollView sv = new ScrollView(this);
        sv.setBackgroundColor(Color.rgb(18, 18, 24));
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(24), dp(16), dp(24), dp(24));
        sv.addView(root);

        text(root, "LEGO Batman", 28, true);

        folderBox = new LinearLayout(this);
        folderBox.setOrientation(LinearLayout.VERTICAL);
        root.addView(folderBox);
        text(folderBox, "Game folder", 20, true);
        folderText = text(folderBox, "", 14, false);
        folderStatus = text(folderBox, "", 14, false);
        Button pick = new Button(this);
        pick.setText("Choose LEGO Batman folder");
        pick.setOnClickListener(v -> {
            /* the game reads the folder with plain file calls: ask for file
             * access first (only shown once), then pick */
            if (hasStorage()) openPicker();
            else { pendingPick = true; requestStorage(); }
        });
        folderBox.addView(pick);

        playButton = new Button(this);
        playButton.setText("Play");
        playButton.setTextSize(22);
        playButton.setOnClickListener(v -> { if (inGame) resume(); else play(); });
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(64));
        lp.topMargin = dp(16);
        root.addView(playButton, lp);
        setContentView(sv);
    }

    /* Storage: the app targets API 29 with legacy storage, so the normal
     * "allow access to files" prompt gives the game plain file access. Only
     * if this Android ignores legacy storage and the folder still can't be
     * read is all-files access asked for (once, automatically). */
    private boolean hasStorage() {
        return checkSelfPermission(Manifest.permission.READ_EXTERNAL_STORAGE) == PackageManager.PERMISSION_GRANTED
                || (Build.VERSION.SDK_INT >= 30 && Environment.isExternalStorageManager());
    }

    private void requestStorage() {
        requestPermissions(new String[] {Manifest.permission.READ_EXTERNAL_STORAGE,
                Manifest.permission.WRITE_EXTERNAL_STORAGE}, REQ_PERM);
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] res) {
        super.onRequestPermissionsResult(req, perms, res);
        if (req != REQ_PERM) return;
        boolean ok = hasStorage();
        if (ok && pendingPick) openPicker();
        if (ok && pendingPlay) play();
        pendingPick = pendingPlay = false;
        refreshFolder();
    }

    /* folder exists but its files can't be read: legacy storage was refused */
    private static boolean unreadable(List<String> bad) {
        for (String b : bad) if (b.startsWith("cannot read")) return true;
        return false;
    }

    private static boolean hasAllFiles() {
        return Build.VERSION.SDK_INT < 30 || Environment.isExternalStorageManager();
    }

    private void requestAllFiles() {
        try {
            startActivity(new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                    Uri.parse("package:" + getPackageName())));
        } catch (Exception e) {
            startActivity(new Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION));
        }
    }

    private void openPicker() {
        startActivityForResult(new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE), REQ_FOLDER);
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req != REQ_FOLDER || res != RESULT_OK || data == null || data.getData() == null) return;
        String path = treeToPath(data.getData());
        if (path == null) {
            folderStatus.setText("That location is not a plain folder on this device; choose one on internal storage or the SD card.");
            return;
        }
        prefs.edit().putString("folder", path).apply();
        refreshFolder();
        if (!askedAllFiles && !hasAllFiles() && unreadable(checkFolder(path))) {
            askedAllFiles = true;
            requestAllFiles();
        }
    }

    /* content://...tree/primary:Games/LEGO Batman -> /storage/emulated/0/Games/LEGO Batman */
    private static String treeToPath(Uri tree) {
        String id;
        try { id = DocumentsContract.getTreeDocumentId(tree); } catch (Exception e) { return null; }
        int c = id.indexOf(':');
        if (c < 0) return null;
        String vol = id.substring(0, c), rel = id.substring(c + 1);
        String base = vol.equalsIgnoreCase("primary") ? Environment.getExternalStorageDirectory().getPath()
                : "/storage/" + vol;
        return rel.isEmpty() ? base : base + "/" + rel;
    }

    private static File findCi(File dir, String name) {
        File f = new File(dir, name);
        if (f.exists()) return f;
        String[] l = dir.list();
        if (l != null) for (String s : l) if (s.equalsIgnoreCase(name)) return new File(dir, s);
        return null;
    }

    /* SHA-1 as hex, cached by path, size and modification time; null if unreadable. */
    private String sha1(File f) {
        String key = "sha1:" + f.getAbsolutePath(), stamp = f.length() + ":" + f.lastModified();
        String cached = prefs.getString(key, null);
        if (cached != null && cached.startsWith(stamp + "=")) return cached.substring(stamp.length() + 1);
        try (InputStream in = new FileInputStream(f)) {
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-1");
            byte[] buf = new byte[1 << 16];
            for (int n; (n = in.read(buf)) > 0; ) md.update(buf, 0, n);
            StringBuilder sb = new StringBuilder();
            for (byte b : md.digest()) sb.append(String.format("%02x", b));
            String hex = sb.toString();
            prefs.edit().putString(key, stamp + "=" + hex).apply();
            return hex;
        } catch (Exception e) {
            return null;
        }
    }

    /* Returns the problems that stop the game from starting (empty when ready). */
    private List<String> checkFolder(String path) {
        List<String> bad = new ArrayList<>();
        if (path == null) { bad.add("no folder chosen"); return bad; }
        File d = new File(path);
        if (!d.isDirectory()) { bad.add("folder not found"); return bad; }
        File dat = findCi(d, "GAME.DAT");
        if (dat == null) bad.add(d.canRead() && d.list() != null ? "GAME.DAT missing" : "cannot read the folder");
        else if (!dat.canRead()) bad.add("cannot read GAME.DAT");
        if (findCi(d, "LEGOBatman.exe") == null) bad.add("LEGOBatman.exe missing");
        for (String n : new String[] {"d3dx9_35.dll", "binkw32.dll"})
            if (findCi(d, n) == null) bad.add(n + " missing");
        return bad;
    }

    /* Problems the game may survive, shown but not blocking Play. */
    private List<String> warnFolder(String path) {
        List<String> warn = new ArrayList<>();
        File d = new File(path);
        File exe = findCi(d, "LEGOBatman.exe");
        if (exe != null) {
            String got = sha1(exe);
            if (got != null && !got.equals(SHA_EXE))
                warn.add("LEGOBatman.exe is a different build (SHA-1 " + got.substring(0, 8) + ", expected "
                        + SHA_EXE.substring(0, 8) + "; strip the Steam version with Steamless). It will probably crash");
        }
        for (String n : new String[] {"HERO1.DAT", "Audio", "Movies"})
            if (findCi(d, n) == null) warn.add(n + " missing");
        return warn;
    }

    private File portDir() {
        String folder = prefs.getString("folder", null);
        return folder == null ? null : new File(folder, "port");
    }

    private void refreshFolder() {
        inGame = LegoBatmanActivity.sRunning;
        folderBox.setVisibility(inGame ? android.view.View.GONE : android.view.View.VISIBLE);
        playButton.setText(inGame ? "Resume" : "Play");
        if (inGame) { playButton.setEnabled(true); return; }
        String path = prefs.getString("folder", null);
        folderText.setText(path == null ? "No folder chosen yet. Copy your LEGO Batman folder (with d3dx9_35.dll) "
                + "anywhere on the phone, e.g. Internal storage/LEGO Batman, then choose it here." : path);
        List<String> bad = checkFolder(path);
        List<String> warn = bad.isEmpty() ? warnFolder(path) : new ArrayList<>();
        if (!bad.isEmpty()) folderStatus.setText("Not ready: " + String.join(", ", bad));
        else folderStatus.setText(warn.isEmpty() ? "Ready" : "Ready, but: " + String.join(", ", warn));
        folderStatus.setTextColor(!bad.isEmpty() ? Color.rgb(230, 150, 110)
                : warn.isEmpty() ? Color.rgb(140, 200, 120) : Color.rgb(230, 200, 110));
        playButton.setEnabled(bad.isEmpty());
    }

    /* Copies the saves of 0.1.6 and older (the app's own folder) into the new
     * save folder, once, if that has none yet. */
    private void migrateSaves(File save) {
        File ext = getExternalFilesDir(null);
        File old = ext == null ? null : new File(ext, "save");
        if (old == null || !old.isDirectory() || old.getAbsoluteFile().equals(save.getAbsoluteFile())) return;
        if (new File(save, "Documents").exists() || new File(save, "touch.cfg").exists()) return;
        copyTree(old, save);
    }

    private static void copyTree(File from, File to) {
        to.mkdirs();
        File[] l = from.listFiles();
        if (l == null) return;
        for (File f : l) {
            File t = new File(to, f.getName());
            if (f.isDirectory()) { copyTree(f, t); continue; }
            try (InputStream in = new FileInputStream(f); OutputStream out = new FileOutputStream(t)) {
                byte[] buf = new byte[1 << 16];
                for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
            } catch (Exception ignored) {
            }
        }
    }

    private void resume() {
        Intent i = new Intent(this, LegoBatmanActivity.class);
        i.addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
        startActivity(i);
        finish();
    }

    @Override
    public void onBackPressed() {
        if (inGame) resume();
        else super.onBackPressed();
    }

    private void play() {
        String folder = prefs.getString("folder", null);
        File ext = getExternalFilesDir(null);
        boolean own = ext != null && folder.startsWith(ext.getPath());  // the app's own folder needs no prompt
        if (!own && !hasStorage()) { pendingPlay = true; requestStorage(); return; }
        File save = new File(portDir(), "save");
        save.mkdirs();
        migrateSaves(save);
        if (!save.isDirectory() || !save.canWrite()) {
            folderStatus.setText("Could not write to " + save);
            folderStatus.setTextColor(Color.rgb(230, 150, 110));
            return;
        }
        Intent i = new Intent(this, LegoBatmanActivity.class);
        i.putExtra("game", folder);
        i.putExtra("save", save.getPath());
        startActivity(i);
        finish();
    }
}

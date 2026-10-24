package com.tj.legobatman;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.Settings;
import android.util.Log;
import android.widget.Toast;
import java.io.File;
import org.libsdl.app.SDLActivity;

public class LegoBatmanActivity extends SDLActivity {
    private static final String TAG = "LegoBatman";

    private static final String LOG_PATH = "/sdcard/LegoBatman/log.txt";

    public static synchronized void resetLog() {
        try {
            File f = new File(LOG_PATH);
            if (f.getParentFile() != null && !f.getParentFile().exists()) {
                f.getParentFile().mkdirs();
            }
            java.io.FileWriter fw = new java.io.FileWriter(f, false);
            fw.write("");
            fw.flush();
            fw.close();
        } catch (Exception ignored) {}
    }

    public static synchronized void writeLog(String message) {
        Log.i(TAG, message);
        try {
            File f = new File(LOG_PATH);
            if (f.getParentFile() != null && !f.getParentFile().exists()) {
                f.getParentFile().mkdirs();
            }
            java.io.FileWriter fw = new java.io.FileWriter(f, true);
            fw.write(message + "\n");
            fw.flush();
            fw.close();
        } catch (Exception ignored) {}
    }

    @Override
    public int messageboxShowMessageBox(
            final int flags,
            final String title,
            final String message,
            final int[] buttonFlags,
            final int[] buttonIds,
            final String[] buttonTexts,
            final int[] colors) {
        writeLog("\n[SDL ERROR WINDOW POPUP]");
        writeLog("Title: " + title);
        writeLog("Message: " + message);
        writeLog("[END ERROR WINDOW POPUP]\n");
        return super.messageboxShowMessageBox(flags, title, message, buttonFlags, buttonIds, buttonTexts, colors);
    }

    @Override
    public void loadLibraries() {
        writeLog("--- Loading native libraries ---");
        try {
            super.loadLibraries();
            writeLog("--- Native libraries loaded successfully ---");
        } catch (Throwable t) {
            writeLog("FATAL: Error loading native libraries: " + t.getMessage());
            java.io.StringWriter sw = new java.io.StringWriter();
            t.printStackTrace(new java.io.PrintWriter(sw));
            writeLog(sw.toString());
            throw t;
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        resetLog();
        writeLog("=== LEGO Batman Session Started ===");
        writeLog("Device: " + Build.MANUFACTURER + " " + Build.MODEL + " (Android " + Build.VERSION.RELEASE + ", API " + Build.VERSION.SDK_INT + ")");
        writeLog("--- LegoBatmanActivity onCreate() ---");
        Thread.setDefaultUncaughtExceptionHandler((thread, throwable) -> {
            try {
                java.io.StringWriter sw = new java.io.StringWriter();
                throwable.printStackTrace(new java.io.PrintWriter(sw));
                writeLog("\n=== UNCAUGHT JAVA EXCEPTION ===\n" + sw.toString() + "===============================\n");
            } catch (Exception ignored) {}
            android.os.Process.killProcess(android.os.Process.myPid());
        });
        super.onCreate(savedInstanceState);
        checkStoragePermission();
    }

    @Override
    protected void onResume() {
        super.onResume();
        checkStoragePermission();
    }

    private void checkStoragePermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (!Environment.isExternalStorageManager()) {
                try {
                    Toast.makeText(this, "Please grant All Files Access to load game data", Toast.LENGTH_LONG).show();
                    Intent intent = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION);
                    intent.setData(Uri.parse("package:" + getPackageName()));
                    startActivity(intent);
                } catch (Exception e) {
                    try {
                        Intent intent = new Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION);
                        startActivity(intent);
                    } catch (Exception ignored) {}
                }
            }
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            if (checkSelfPermission(Manifest.permission.READ_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED ||
                checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{
                    Manifest.permission.READ_EXTERNAL_STORAGE,
                    Manifest.permission.WRITE_EXTERNAL_STORAGE
                }, 100);
            }
        }
    }

    private boolean containsGameFiles(File dir) {
        if (dir == null || !dir.exists() || !dir.isDirectory()) {
            return false;
        }
        File[] files = dir.listFiles();
        if (files == null) {
            return false;
        }
        for (File f : files) {
            String name = f.getName().toLowerCase();
            if (name.equals("legobatman.exe") || name.equals("game.dat")) {
                return true;
            }
        }
        return false;
    }

    private File resolveGameDirectory() {
        File extDir = Environment.getExternalStorageDirectory();
        File[] candidates = new File[] {
            new File(extDir, "LegoBatman"),
            new File(extDir, "LegoBatman/game"),
            new File("/sdcard/LegoBatman"),
            new File("/sdcard/LegoBatman/game"),
            new File("/storage/emulated/0/LegoBatman"),
            new File("/storage/emulated/0/LegoBatman/game")
        };

        for (File candidate : candidates) {
            if (containsGameFiles(candidate)) {
                Log.i(TAG, "Found game files in: " + candidate.getAbsolutePath());
                return candidate;
            }
        }

        // Check legacy paths under Android/data/.../files/game
        try {
            File legacyExt = super.getExternalFilesDir(null);
            if (legacyExt != null) {
                File legacyGame = new File(legacyExt, "game");
                if (containsGameFiles(legacyGame)) {
                    Log.i(TAG, "Found game files in legacy path: " + legacyGame.getAbsolutePath());
                    return legacyGame;
                }
                if (containsGameFiles(legacyExt)) {
                    Log.i(TAG, "Found game files in legacy files dir: " + legacyExt.getAbsolutePath());
                    return legacyExt;
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "Failed to check legacy external files dir: " + e.getMessage());
        }

        // Default to LegoBatman (or LegoBatman/game if game folder exists)
        File defaultDir = new File(extDir, "LegoBatman");
        File gameSub = new File(defaultDir, "game");
        if (gameSub.exists() && gameSub.isDirectory()) {
            return gameSub;
        }
        defaultDir.mkdirs();
        return defaultDir;
    }

    @Override
    protected String[] getArguments() {
        File gameDir = resolveGameDirectory();
        File internalDir = new File(Environment.getExternalStorageDirectory(), "LegoBatman");
        File saveDir = new File(internalDir, "save");
        if (!saveDir.exists()) {
            saveDir.mkdirs();
        }

        Log.i(TAG, "Arguments: game_dir=" + gameDir.getAbsolutePath() + ", save_dir=" + saveDir.getAbsolutePath());
        return new String[] { gameDir.getAbsolutePath(), saveDir.getAbsolutePath() };
    }

    @Override
    public File getExternalFilesDir(String type) {
        try {
            File base = new File(Environment.getExternalStorageDirectory(), "LegoBatman");
            if (!base.exists()) {
                base.mkdirs();
            }
            if (type != null) {
                File sub = new File(base, type);
                if (!sub.exists()) {
                    sub.mkdirs();
                }
                return sub;
            }
            return base;
        } catch (Exception e) {
            return super.getExternalFilesDir(type);
        }
    }

    @Override
    protected String[] getLibraries() {
        return new String[] { "SDL2", "dxvk_d3d9", "main" };
    }
}

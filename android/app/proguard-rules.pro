# ProGuard and R8 optimization rules for LEGO Batman Android

# SDL2 JNI bindings and Activity lifecycle (mandatory for JNI calls from libSDL2.so)
-keep class org.libsdl.app.** { *; }
-keep class com.tj.legobatman.** { *; }

# Keep all native methods and their enclosing classes
-keepclasseswithmembernames class * {
    native <methods>;
}

# Keep reflection methods called by SDL2 C code
-keepclassmembers class * {
    public static java.lang.Object getContext();
    public java.io.File getExternalFilesDir(java.lang.String);
}

# Preserve line numbers and attributes for clean debugging
-keepattributes SourceFile,LineNumberTable,*Annotation*,Signature,InnerClasses,EnclosingMethod
-dontwarn org.libsdl.app.**

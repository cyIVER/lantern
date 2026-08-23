#!/bin/bash
# Normalise staged CounterStrikeSharp plugin sets into a uniform overlay.
#
# Every plugin ships a differently-shaped archive:
#
#   matchzy       addons/ cfg/                    -> rooted at csgo/
#   retakes       addons/                         -> rooted at csgo/
#   practice      "Linux Release vX"/addons/ cfg/ -> rooted at csgo/, nested one level
#   simpleadmin   counterstrikesharp/             -> rooted at csgo/addons/
#   weaponpaints  WeaponPaints/ gamedata/         -> loose, rooted at csgo/addons/counterstrikesharp/
#
# After this runs, every set is a tree rooted at csgo/, so activation is a plain
# copy. Each set also gets a .lantern-plugins manifest listing the plugin
# directory names it owns, which lets boot.sh deactivate a set precisely instead
# of wiping the whole plugins directory.
#
#   normalize-plugins.sh <plugins.available dir>
set -uo pipefail

STAGE="${1:?staging dir required}"
cd "$STAGE" || exit 1

# CSSharp directories that live directly under addons/counterstrikesharp/.
is_css_dir() {
    case "$1" in
        plugins|shared|gamedata|configs|dotnet|api|lang) return 0 ;;
        *) return 1 ;;
    esac
}

for set_dir in */; do
    set_name="${set_dir%/}"
    [ -d "$set_name" ] || continue
    echo "  [$set_name]"

    # 1. Descend through wrapper directories (practice ships "Linux Release vX").
    while :; do
        entries=$(ls -A "$set_name" 2>/dev/null)
        count=$(printf '%s\n' "$entries" | grep -c . || true)
        [ "$count" = "1" ] || break
        only="$entries"
        [ -d "$set_name/$only" ] || break
        case "$only" in addons|cfg|counterstrikesharp|gamedata) break ;; esac
        echo "      descending into '$only'"
        mv "$set_name/$only" "$set_name/.__lift" && \
        rm -rf "${set_name:?}"/* 2>/dev/null
        mv "$set_name/.__lift"/* "$set_name/" 2>/dev/null
        rmdir "$set_name/.__lift" 2>/dev/null
    done

    # 2. Reshape whatever is at the top into a csgo/-rooted tree.
    if [ -d "$set_name/addons" ]; then
        echo "      already csgo-rooted"
    elif [ -d "$set_name/counterstrikesharp" ]; then
        echo "      wrapping counterstrikesharp/ into addons/"
        mkdir -p "$set_name/addons"
        mv "$set_name/counterstrikesharp" "$set_name/addons/"
    else
        echo "      loose layout, sorting into addons/counterstrikesharp/"
        target="$set_name/addons/counterstrikesharp"
        mkdir -p "$target/plugins"
        for item in "$set_name"/*; do
            base=$(basename "$item")
            [ "$base" = "addons" ] && continue
            [ "$base" = "cfg" ] && continue
            if is_css_dir "$base"; then
                mv "$item" "$target/$base"
            else
                mv "$item" "$target/plugins/$base"
            fi
        done
    fi

    # 2a. Strip any Metamod the archive carries.
    #
    # CS2-Practice-Plugin ships a full addons/metamod plus its .vdf files. The
    # overlay would copy that over the platform's Metamod, replacing a current
    # build with whatever stale one the plugin was packaged against -- which
    # produces, on the next boot:
    #
    #   MMS: Fatal error: Detected engine 26 but could not load:
    #     metamod.2.cs2.so: undefined symbol: UtlMemory_CalcNewAllocationCount
    #
    # Metamod then never loads, so CounterStrikeSharp never loads, so every
    # plugin silently does nothing. CSSharp plugins are loaded by CSSharp, not
    # by Metamod, so none of them has any business shipping one.
    if [ -e "$set_name/addons/metamod" ] || ls "$set_name/addons/"metamod*.vdf >/dev/null 2>&1; then
        echo "      stripping bundled Metamod (would clobber the real one)"
        rm -rf "$set_name/addons/metamod"
        rm -f "$set_name/addons/"metamod*.vdf
    fi

    # Same problem one layer up: CS2-Practice-Plugin ships a complete
    # CounterStrikeSharp distribution (bin/, dotnet/, api/, source/). Overlaying
    # that replaces the live runtime with whatever it was packaged against:
    #
    #   [META] Failed to load counterstrikesharp.so:
    #     undefined symbol: _ZN24CUtlMemoryBlockAllocator5PurgeEv
    #
    # A plugin set may only contribute plugins/, shared/, gamedata/, configs/
    # and lang/. Everything else under addons/counterstrikesharp/ is platform.
    css="$set_name/addons/counterstrikesharp"
    if [ -d "$css" ]; then
        for item in "$css"/*; do
            [ -e "$item" ] || continue
            case "$(basename "$item")" in
                plugins|shared|gamedata|configs|lang) ;;
                *) echo "      stripping platform dir: $(basename "$item")"
                   rm -rf "$item" ;;
            esac
        done

        # gamedata/ and configs/ are SHARED directories: plugins legitimately add
        # their own files (weaponpaints.json), but the platform owns specific
        # ones. Practice ships the core gamedata.json -- the engine signature
        # table. Overwriting it with a 2024 copy makes CSSharp fail to find
        # Host_Say, GetLegacyGameEventListener and friends, then segfault the
        # whole server. Strip at file level, not directory level.
        for core in gamedata.json schema_classes.txt schema_enums.txt; do
            if [ -e "$css/gamedata/$core" ]; then
                echo "      stripping platform gamedata: $core"
                rm -f "$css/gamedata/$core"
            fi
        done

        # In configs/, only the plugins/ subdirectory belongs to plugins; the
        # rest (core.example.json, admins.example.json, ...) is platform.
        if [ -d "$css/configs" ]; then
            for item in "$css/configs"/*; do
                [ -e "$item" ] || continue
                if [ "$(basename "$item")" != "plugins" ]; then
                    echo "      stripping platform config: $(basename "$item")"
                    rm -rf "$item"
                fi
            done
        fi
    fi

    # 2b. Drop loose files from plugins/. CSSharp only loads directories, but a
    # stray README.txt sitting there shows up in the "active plugins" line and
    # makes that diagnostic misleading.
    if [ -d "$set_name/addons/counterstrikesharp/plugins" ]; then
        find "$set_name/addons/counterstrikesharp/plugins" -maxdepth 1 -type f -delete 2>/dev/null
    fi

    # 3. Record which plugin directories this set owns.
    # Directories only: loose files like README.txt are not plugins, and
    # CSSharp's shared 'disabled' directory must never be attributed to one set.
    manifest="$set_name/.lantern-plugins"
    : > "$manifest"
    plug_dir="$set_name/addons/counterstrikesharp/plugins"
    if [ -d "$plug_dir" ]; then
        for p in "$plug_dir"/*/; do
            [ -d "$p" ] || continue
            name=$(basename "$p")
            [ "$name" = "disabled" ] && continue
            echo "$name" >> "$manifest"
        done
    fi
    echo "      owns plugins: $(tr '\n' ' ' < "$manifest")"
done

echo "  normalisation complete"

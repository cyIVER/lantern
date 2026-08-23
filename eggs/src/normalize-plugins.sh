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

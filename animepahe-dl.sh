#!/usr/bin/env bash
#
# Download anime from animepahe in terminal
#
# CONFIGURATION: Fake User Agent to bypass Kwik/Cloudflare blocks
_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
# MAX SIZE LIMIT (in MB)
_MAX_SIZE_MB=500

set -e
set -u

usage() {
    echo "Usage: ./animepahe-dl.sh [-a <name>] [-e <ep>] [-r <res>] [-t <threads>] [-d]"
    exit 1
}

set_var() {
    _CURL_CMD="$(command -v curl)" || { echo "curl not found"; exit 1; }
    _JQ="$(command -v jq)" || { echo "jq not found"; exit 1; }
    _FZF="head -n 1"
    
    if [[ -z ${ANIMEPAHE_DL_NODE:-} ]]; then
        _NODE="$(command -v node)" || { echo "node not found"; exit 1; }
    else
        _NODE="$ANIMEPAHE_DL_NODE"
    fi
    _FFMPEG="$(command -v ffmpeg)" || { echo "ffmpeg not found"; exit 1; }
    _OPENSSL="$(command -v openssl)" || { echo "openssl not found"; exit 1; }

    _HOST="https://animepahe.si"
    _ANIME_URL="$_HOST/anime"
    _API_URL="$_HOST/api"
    _REFERER_URL="https://kwik.cx/"

    _SCRIPT_PATH=$(dirname "$(realpath "$0")")
    _ANIME_LIST_FILE="$_SCRIPT_PATH/anime.list"
    _SOURCE_FILE=".source.json"
}

set_args() {
    _PARALLEL_JOBS=1
    while getopts ":hlda:s:e:r:t:o:" opt; do
        case $opt in
            a) _INPUT_ANIME_NAME="$OPTARG" ;;
            s) _ANIME_SLUG="$OPTARG" ;;
            e) _ANIME_EPISODE="$OPTARG" ;;
            l) _LIST_LINK_ONLY=true ;;
            r) _ANIME_RESOLUTION="$OPTARG" ;;
            t) _PARALLEL_JOBS="$OPTARG" ;;
            o) _ANIME_AUDIO="$OPTARG" ;;
            d) _DEBUG_MODE=true; set -x ;;
            h) usage ;;
            \?) echo "Invalid option: -$OPTARG"; exit 1 ;;
        esac
    done
}

print_info() { [[ -z "${_LIST_LINK_ONLY:-}" ]] && printf "%b\n" "\033[32m[INFO]\033[0m $1" >&2; }
print_warn() { [[ -z "${_LIST_LINK_ONLY:-}" ]] && printf "%b\n" "\033[33m[WARNING]\033[0m $1" >&2; }
print_error() { printf "%b\n" "\033[31m[ERROR]\033[0m $1" >&2; exit 1; }

curl_req() { "$_CURL_CMD" -H "User-Agent: $_USER_AGENT" "$@"; }
get() { curl_req -sS -L "$1" -H "cookie: $_COOKIE" --compressed; }

set_cookie() {
    local u
    u="$(LC_ALL=C tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 16)"
    _COOKIE="__ddg2_=$u"
}

download_anime_list() {
    get "$_ANIME_URL" | grep "/anime/" | sed -E 's/.*anime\//[/;s/" title="/] /;s/\">.*/    /;s/" title/]/' > "$_ANIME_LIST_FILE"
}

search_anime_by_name() {
    local d n
    d="$(get "$_HOST/api?m=search&q=${1// /%20}")"
    n="$("$_JQ" -r '.total' <<< "$d")"
    if [[ "$n" -eq "0" ]]; then echo ""; else
        "$_JQ" -r '.data[] | "[\(.session)] \(.title)    "' <<< "$d" | tee -a "$_ANIME_LIST_FILE" | awk -F'] ' '{print $2}'
    fi
}

get_episode_list() { get "${_API_URL}?m=release&id=${1}&sort=episode_asc&page=${2}"; }

download_source() {
    local d p n
    mkdir -p "$_SCRIPT_PATH/$_ANIME_NAME"
    d="$(get_episode_list "$_ANIME_SLUG" "1")"
    p="$("$_JQ" -r '.last_page' <<< "$d")"
    if [[ "$p" -gt "1" ]]; then
        for i in $(seq 2 "$p"); do
            n="$(get_episode_list "$_ANIME_SLUG" "$i")"
            d="$(echo "$d $n" | "$_JQ" -s '.[0].data + .[1].data | {data: .}')"
        done
    fi
    echo "$d" > "$_SCRIPT_PATH/$_ANIME_NAME/$_SOURCE_FILE"
}

get_episode_link() {
    local s o l r="" size_str=""
    s=$("$_JQ" -r '.data[] | select((.episode | tonumber) == ($num | tonumber)) | .session' --arg num "$1" < "$_SCRIPT_PATH/$_ANIME_NAME/$_SOURCE_FILE")
    [[ "$s" == "" ]] && print_warn "Episode $1 not found!" && return
    
    o="$(curl_req --compressed -sSL -H "cookie: $_COOKIE" "${_HOST}/play/${_ANIME_SLUG}/${s}")"
    l="$(grep \<button <<< "$o" | grep data-src | sed -E 's/data-src="/\n/g' | grep 'data-av1="0"')"

    if [[ -n "${_ANIME_RESOLUTION:-}" ]]; then
        print_info "Select video resolution: $_ANIME_RESOLUTION"
        r="$(grep 'data-resolution="'"$_ANIME_RESOLUTION"'"' <<< "${r:-$l}")"
    fi

    # Determine final HTML line to parse
    local final_line
    if [[ -z "${r:-}" ]]; then
        final_line=$(grep kwik <<< "$l" | tail -1)
    else
        final_line=$(tail -1 <<< "$r")
    fi

    # --- SIZE CHECK GUARD ---
    # Extract size string e.g., (150MB) or (1.2GB)
    if [[ "$final_line" =~ \(([0-9.]+)(MB|GB)\) ]]; then
        local size_val=${BASH_REMATCH[1]}
        local size_unit=${BASH_REMATCH[2]}
        local size_mb=0

        # Convert float to integer for safe comparison
        size_val=$(printf "%.0f" "$size_val")

        if [[ "$size_unit" == "GB" ]]; then
            # Any GB is definitely > 500MB
            print_warn "⚠️ SKIPPING: File size is in GB ($size_val GB). Too large for server."
            exit 2
        elif [[ "$size_unit" == "MB" ]]; then
            if [[ "$size_val" -gt "$_MAX_SIZE_MB" ]]; then
                print_warn "⚠️ SKIPPING: File size $size_val MB is larger than limit $_MAX_SIZE_MB MB."
                exit 2
            else
                print_info "✅ File size check passed: $size_val MB"
            fi
        fi
    fi
    # ------------------------

    # Return URL
    awk -F '" ' '{print $1}' <<< "$final_line"
}

get_playlist_link() {
    local s l
    s="$(curl_req --compressed -sS -H "Referer: $_REFERER_URL" -H "cookie: $_COOKIE" "$1" \
        | grep "<script>eval(" | awk -F 'script>' '{print $2}'\
        | sed -E 's/document/process/g' | sed -E 's/querySelector/exit/g' | sed -E 's/eval\(/console.log\(/g')"
    l="$("$_NODE" -e "$s" | grep 'source=' | sed -E "s/.m3u8';.*/.m3u8/" | sed -E "s/.*const source='//")"
    echo "$l"
}

get_thread_number() {
    local sn
    sn="$(grep -c "^https" "$1")"
    if [[ "$sn" -lt "$_PARALLEL_JOBS" ]]; then echo "$sn"; else echo "$_PARALLEL_JOBS"; fi
}

download_file() {
    local s
    s=$(curl_req -k -sS -H "Referer: $_REFERER_URL" -H "cookie: $_COOKIE" -C - "$1" -L -g -o "$2" --connect-timeout 5 --compressed || echo "$?")
    if [[ "$s" -ne 0 ]]; then
        print_warn "Download was aborted. Retry..."
        download_file "$1" "$2"
    fi
}

decrypt_file() {
    local of=${1%%.encrypted}
    "$_OPENSSL" aes-128-cbc -d -K "$2" -iv 0 -in "${1}" -out "${of}" 2>/dev/null
}

download_segments() {
    local op="$2"
    export _CURL_CMD _USER_AGENT _REFERER_URL op
    export -f download_file curl_req print_warn
    xargs -I {} -P "$(get_thread_number "$1")" bash -c 'url="{}"; file="${url##*/}.encrypted"; download_file "$url" "${op}/${file}"' < <(grep "^https" "$1")
}

generate_filelist() { grep "^https" "$1" | sed -E "s/https.*\//file '/" | sed -E "s/$/'/" > "$2"; }

decrypt_segments() {
    local kf kl k
    kf="${2}/mon.key"
    kl=$(grep "#EXT-X-KEY:METHOD=" "$1" | awk -F '"' '{print $2}')
    download_file "$kl" "$kf"
    k="$(od -A n -t x1 "$kf" | tr -d ' \n')"
    export _OPENSSL k
    export -f decrypt_file
    xargs -I {} -P "$(get_thread_number "$1")" bash -c 'decrypt_file "{}" "$k"' < <(ls "${2}/"*.encrypted)
}

download_episode() {
    local num="$1" l pl v erropt='' 
    v="$_SCRIPT_PATH/${_ANIME_NAME}/${num}.mp4"

    l=$(get_episode_link "$num")
    # Capture exit code 2 (Size Limit) from subshell
    if [[ $? -eq 2 ]]; then exit 2; fi

    [[ "$l" != *"/"* ]] && print_warn "Wrong download link or episode $1 not found!" && return
    pl=$(get_playlist_link "$l")
    [[ -z "${pl:-}" ]] && print_warn "Missing video list! Skip downloading!" && return

    if [[ -z ${_LIST_LINK_ONLY:-} ]]; then
        print_info "Downloading Episode $1..."
        [[ -z "${_DEBUG_MODE:-}" ]] && erropt="-v error"

        local opath plist cpath fname
        fname="file.list"
        cpath="$(pwd)"
        opath="$_SCRIPT_PATH/$_ANIME_NAME/${num}"
        plist="${opath}/playlist.m3u8"
        rm -rf "$opath"; mkdir -p "$opath"

        download_file "$pl" "$plist"
        print_info "Start parallel jobs with $(get_thread_number "$plist") threads"
        download_segments "$plist" "$opath"
        decrypt_segments "$plist" "$opath"
        generate_filelist "$plist" "${opath}/$fname"

        ! cd "$opath" && print_warn "Cannot change directory to $opath" && return
        "$_FFMPEG" -f concat -safe 0 -i "$fname" -c copy $erropt -y "$v"
        ! cd "$cpath" && print_warn "Cannot change directory to $cpath" && return
        [[ -z "${_DEBUG_MODE:-}" ]] && rm -rf "$opath" || return 0
    else
        echo "$pl"
    fi
}

select_episodes_to_download() {
    "$_JQ" -r '.data[] | "[\(.episode | tonumber)] E\(.episode | tonumber) \(.created_at)"' "$_SCRIPT_PATH/$_ANIME_NAME/$_SOURCE_FILE" >&2
    echo -n "Which episode(s) to download: " >&2; read -r s; echo "$s"
}

remove_brackets() { awk -F']' '{print $1}' | sed -E 's/^\[//'; }
remove_slug() { awk -F'] ' '{print $2}'; }
get_slug_from_name() { grep "] $1" "$_ANIME_LIST_FILE" | tail -1 | remove_brackets; }

download_episodes() {
    local origel el uniqel
    origel=()
    if [[ "$1" == *","* ]]; then
        IFS="," read -ra ADDR <<< "$1"; for n in "${ADDR[@]}"; do origel+=("$n"); done
    else origel+=("$1"); fi

    el=()
    for i in "${origel[@]}"; do
        if [[ "$i" == *"*"* ]]; then
            local eps fst lst
            eps="$("$_JQ" -r '.data[].episode' "$_SCRIPT_PATH/$_ANIME_NAME/$_SOURCE_FILE" | sort -nu)"
            fst="$(head -1 <<< "$eps")"; lst="$(tail -1 <<< "$eps")"; i="${fst}-${lst}"
        fi
        if [[ "$i" == *"-"* ]]; then
            s=$(awk -F '-' '{print $1}' <<< "$i"); e=$(awk -F '-' '{print $2}' <<< "$i")
            for n in $(seq "$s" "$e"); do el+=("$n"); done
        else el+=("$i"); fi
    done
    IFS=" " read -ra uniqel <<< "$(printf '%s\n' "${el[@]}" | sort -n -u | tr '\n' ' ')"
    for e in "${uniqel[@]}"; do download_episode "$e"; done
}

main() {
    set_args "$@"
    set_var; set_cookie
    if [[ -n "${_INPUT_ANIME_NAME:-}" ]]; then
        _ANIME_NAME=$(search_anime_by_name "$_INPUT_ANIME_NAME" | head -n 1)
        _ANIME_SLUG="$(get_slug_from_name "$_ANIME_NAME")"
    else download_anime_list; fi

    [[ "$_ANIME_SLUG" == "" ]] && print_error "Anime slug not found!"
    _ANIME_NAME="$(grep "$_ANIME_SLUG" "$_ANIME_LIST_FILE" | tail -1 | remove_slug | sed -E 's/[[:space:]]+$//' | sed -E 's/[^[:alnum:] ,\+\-\)\(]/_/g')"
    if [[ "$_ANIME_NAME" == "" ]]; then print_warn "Anime name not found!"; exit 1; fi

    download_source
    [[ -z "${_ANIME_EPISODE:-}" ]] && _ANIME_EPISODE=$(select_episodes_to_download)
    download_episodes "$_ANIME_EPISODE"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then main "$@"; fi

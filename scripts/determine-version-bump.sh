#!/bin/bash
set -euo pipefail

# Function to compare semver and determine bump type
compare_versions() {
    local old_version="$1"
    local new_version="$2"

    # Parse major.minor.patch
    IFS='.' read -ra OLD <<< "$old_version"
    IFS='.' read -ra NEW <<< "$new_version"

    # Compare major
    if [[ "${NEW[0]}" -gt "${OLD[0]}" ]]; then
        echo "major"
    # Compare minor
    elif [[ "${NEW[1]}" -gt "${OLD[1]}" ]]; then
        echo "minor"
    # Otherwise patch
    else
        echo "patch"
    fi
}

# Extract version changes from Dockerfile diff
extract_version_changes() {
    local dockerfile="$1"

    # Get diff of ARG lines
    local diff_output
    diff_output=$(git diff HEAD~1 -- "$dockerfile" | grep "^[+-]ARG.*_VERSION=" || true)

    # Extract old/new versions for each dependency
    local old_rclone new_rclone old_kopia new_kopia old_alpine new_alpine
    old_rclone=$(echo "$diff_output" | grep "^-ARG RCLONE_VERSION=" | cut -d'=' -f2 || echo "")
    new_rclone=$(echo "$diff_output" | grep "^+ARG RCLONE_VERSION=" | cut -d'=' -f2 || echo "")
    old_kopia=$(echo "$diff_output" | grep "^-ARG KOPIA_VERSION=" | cut -d'=' -f2 || echo "")
    new_kopia=$(echo "$diff_output" | grep "^+ARG KOPIA_VERSION=" | cut -d'=' -f2 || echo "")

    # Also check for Alpine base image changes in FROM lines
    local alpine_diff
    alpine_diff=$(git diff HEAD~1 -- "$dockerfile" | grep "^[+-]FROM alpine:" || true)
    old_alpine=$(echo "$alpine_diff" | grep "^-FROM alpine:" | sed 's/^-FROM alpine://' || echo "")
    new_alpine=$(echo "$alpine_diff" | grep "^+FROM alpine:" | sed 's/^+FROM alpine://' || echo "")

    # Determine highest bump type needed
    # Default to patch for workflow/script changes (no dependency updates)
    local bump_type="patch"
    local changes=()

    if [[ -n "$old_rclone" && -n "$new_rclone" ]]; then
        local rclone_bump
        rclone_bump=$(compare_versions "$old_rclone" "$new_rclone")
        changes+=("rclone: $old_rclone → $new_rclone ($rclone_bump)")

        # Take highest bump priority: major > minor > patch
        if [[ "$rclone_bump" == "major" ]]; then
            bump_type="major"
        elif [[ "$rclone_bump" == "minor" && "$bump_type" != "major" ]]; then
            bump_type="minor"
        fi
    fi

    if [[ -n "$old_kopia" && -n "$new_kopia" ]]; then
        local kopia_bump
        kopia_bump=$(compare_versions "$old_kopia" "$new_kopia")
        changes+=("kopia: $old_kopia → $new_kopia ($kopia_bump)")

        # Take highest bump priority: major > minor > patch
        if [[ "$kopia_bump" == "major" ]]; then
            bump_type="major"
        elif [[ "$kopia_bump" == "minor" && "$bump_type" != "major" ]]; then
            bump_type="minor"
        fi
    fi

    if [[ -n "$old_alpine" && -n "$new_alpine" ]]; then
        local alpine_bump
        alpine_bump=$(compare_versions "$old_alpine" "$new_alpine")
        changes+=("alpine: $old_alpine → $new_alpine ($alpine_bump)")

        # Take highest bump priority: major > minor > patch
        if [[ "$alpine_bump" == "major" ]]; then
            bump_type="major"
        elif [[ "$alpine_bump" == "minor" && "$bump_type" != "major" ]]; then
            bump_type="minor"
        fi
    fi

    # Output for GitHub Actions
    echo "bump_type=$bump_type"
    if [[ ${#changes[@]} -gt 0 ]]; then
        echo "changes=${changes[*]}"
    else
        echo "changes=Container and workflow updates"
    fi
    # Always create releases since we only merge meaningful changes
    echo "should_create_release=true"
}

# Main execution
main() {
    local dockerfile="${1:-proton-drive-backup/Dockerfile}"

    if [[ ! -f "$dockerfile" ]]; then
        echo "Error: Dockerfile not found at $dockerfile" >&2
        exit 1
    fi

    extract_version_changes "$dockerfile"
}

# Run main function with arguments
main "$@"
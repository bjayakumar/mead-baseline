MEAD_CONFIG_LOC='../mead/config/mead-settings.json'
if [ ! -e $MEAD_CONFIG_LOC ]; then
    echo -e "{\"datacache\": \"$HOME/.bl-data\"}" > $MEAD_CONFIG_LOC
fi

CON_BUILD=baseline-${1}
docker build --network=host -t ${CON_BUILD} -f Dockerfile.${1} ../
if [ $? -ne 0 ]; then
    echo "could not build container, exiting"
    exit 1
fi

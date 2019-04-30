import os
import math
import logging
import shutil
from math import ceil

import fiona
import rasterio
from rasterio import features
from rasterio.features import shapes
from osgeo import gdal
import numpy as np
from skimage.filters.rank import majority
from skimage.morphology import rectangle, remove_small_objects
from skimage.measure import label

import bcdata
from becmodel import util
from becmodel.config import config


log = logging.getLogger(__name__)


def validate(config_file):
    if config_file:
        util.load_config(config_file)
    util.load_tables()


def load(config_file=None, overwrite=False):
    """ Read config, load input data, do model calculations
    """
    # load config
    if config_file:
        util.load_config(config_file)

    # load and validate inputs (rule polys, elevation table, becmaster)
    data = util.load_tables()

    # get bounds from gdf and align to Hectares BC raster
    data["bounds"] = util.align(list(data["rulepolys"].geometry.total_bounds))

    # confirm workspace exists, overwrite if specified
    if overwrite and os.path.exists(config["wksp"]):
        shutil.rmtree(config["wksp"])
    util.make_sure_path_exists(config["wksp"])

    # get dem, generate slope and aspect (these are always written to file)
    if not os.path.exists(os.path.join(config["wksp"], "dem.tif")):
        bcdata.get_dem(
            data["bounds"],
            os.path.join(config["wksp"], "dem.tif"),
            resolution=config["cell_size"]
        )

    if not os.path.exists(os.path.join(config["wksp"], "slope.tif")):
        gdal.DEMProcessing(
            os.path.join(config["wksp"], "slope.tif"),
            os.path.join(config["wksp"], "dem.tif"),
            "slope",
            slopeFormat="percent"
        )

    if not os.path.exists(os.path.join(config["wksp"], "aspect.tif")):
        gdal.DEMProcessing(
            os.path.join(config["wksp"], "aspect.tif"),
            os.path.join(config["wksp"], "dem.tif"),
            "aspect",
            zeroForFlat=True
        )

    # load slope from file
    with rasterio.open(os.path.join(config["wksp"], "slope.tif")) as src:
        data["slope"] = src.read(1)

    # load and classify aspect
    with rasterio.open(os.path.join(config["wksp"], "aspect.tif")) as src:
        array1 = src.read(1)
        # set aspect to -1 for all slopes less that 15%
        array1[data["slope"] < config["flat_aspect_slope_threshold"]] = -1
        data["aspect_class"] = array1.copy()
        profile = src.profile
        for aspect in config["aspects"]:
            for rng in aspect["ranges"]:
                data["aspect_class"][
                    (array1 >= rng["min"]) & (array1 < rng["max"])
                ] = aspect["code"]

    # load dem into memory and get the shape / transform
    # (so new rasters line up)
    with rasterio.open(os.path.join(config["wksp"], "dem.tif")) as src:
        shape = src.shape
        transform = src.transform
        height = src.height
        width = src.width
        crs = src.crs
        data["dem"] = src.read(1)

    # burn rule polygon number to raster using DEM shape/transform
    data["rules_image"] = features.rasterize(
        ((geom, value) for geom, value in zip(data["rulepolys"].geometry, data["rulepolys"].polygon_number)),
        out_shape=shape,
        transform=transform,
        all_touched=False,
        dtype=np.uint16,
    )

    # generate becvalue raster by iterating through elevation table,
    # setting output raster to becvalue for each row where criteria are met
    # by the dem/aspect/rulepolys
    data["becvalue_image"] = np.zeros(shape=shape, dtype="uint16")
    for index, row in data["elevation"].iterrows():
        for aspect in config["aspects"]:
            data["becvalue_image"][
                (data["rules_image"] == row["polygon_number"]) &
                (data["aspect_class"] == aspect["code"]) &
                (data["dem"] >= row[aspect["name"]+"_low"]) &
                (data["dem"] < row[aspect["name"]+"_high"])
            ] = row["becvalue"]

    # Smooth by applying majority filter to output
    # Note that skimage.filters.rank.majority is currently unreleased
    low_slope_size = ceil((config["majority_filter_low_slope_size"] / config["cell_size"]))
    steep_slope_size = ceil((config["majority_filter_steep_slope_size"] / config["cell_size"]))
    data["becvalue_filtered"] = np.where(
        data["slope"] < config["majority_filter_steep_slope_threshold"],
        majority(data["becvalue_image"],
                 rectangle(width=low_slope_size, height=low_slope_size)),
        majority(data["becvalue_image"],
                 rectangle(width=steep_slope_size, height=steep_slope_size))
    )

    # Remove areas smaller than noise removal threshold
    # first, convert noise_removal_threshold value from m2 to n cells
    noise_threshold = int(
        config["noise_removal_threshold"] / (config["cell_size"] **2)
    )

    # now find unique cell groupings (like converting to singlepart)
    data["becvalue_labels"] = label(data["becvalue_filtered"], connectivity=1)

    # identify the areas smaller than noise removal threshold
    data["mask"] = remove_small_objects(
        data["becvalue_labels"],
        noise_threshold
    )

    # Fill in the masked areas by again applying a majority filter.
    # But, this time,
    # - exclude areas smaller than noise threshold from majority filter calc
    # - only use the result to fill in the holes
    data["becvalue_cleaned"] = np.where(
        (data["mask"] == 0) & (data["becvalue_filtered"] > 0),
        majority(data["becvalue_filtered"], rectangle(height=10, width=10), mask=data["mask"]),
        data["becvalue_filtered"]
    )
    return data


def write(data, qa=False):
    """ Write outputs to disk
    """
    # read DEM to get crs / width / height etc
    with rasterio.open(os.path.join(config["wksp"], "dem.tif")) as src:

        if qa:
            for raster in [
                "becvalue_image",
                "becvalue_filtered",
                "becvalue_labels",
                "mask",
                "becvalue_cleaned",
                "rules_image",
                "aspect_class"
            ]:
                with rasterio.open(
                    os.path.join(config["wksp"], raster+".tif"),
                    "w",
                    driver="GTiff",
                    dtype=rasterio.uint16,
                    count=1,
                    width=src.width,
                    height=src.height,
                    crs=src.crs,
                    transform=src.transform,
                ) as dst:
                    dst.write(data[raster].astype(np.uint16), indexes=1)

        # write output vectors to file
        results = (
                {"properties": {"becvalue": v}, "geometry": s}
                for i, (s, v)
                in enumerate(
                    shapes(data["becvalue_cleaned"], transform=src.transform))
        )
        with fiona.open(
                os.path.join(config["wksp"], config["out_file"]),
                "w",
                layer=config["out_layer"],
                driver="GPKG",
                crs=src.crs,
                schema={"properties": [("becvalue", "int")],
                        "geometry": "Polygon"}) as dst:
            dst.writerecords(results)
        log.info("Output {} created".format(os.path.join(config["wksp"], config["out_file"])))

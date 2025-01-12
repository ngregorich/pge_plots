import argparse
import logging
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import re
import streamlit as st
from dataclasses import dataclass
from io import BytesIO
from meteostat import Point, Hourly
from plotly.subplots import make_subplots
from typing import BinaryIO, List, Optional, Tuple
from uszipcode import SearchEngine

parser = argparse.ArgumentParser(description="Change logging level using a command-line argument.")
parser.add_argument(
    "--level",
    type=str,
    choices=["INFO", "WARNING"],
    default="WARNING",
    help="Set the logging level (default: WARNING)",
)

args = parser.parse_args()

logger = logging.getLogger(__name__)

logging.basicConfig(
    format="%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s",
    level=getattr(logging, args.level),
    datefmt="%Y-%m-%d %H:%M:%S",
)

electric_units = "kWh"
local_tz = "America/Los_Angeles"
filename = (
    "data/pge_electric_usage_interval_data_Service 1_1_2023-12-19_to_2024-12-19.csv"
)

# make white / alpha channel to overlay high energy on weather temperature
alpha_scale = [
    [0.0, "rgba(255, 255, 255, 0)"],  # Fully transparent white (alpha = 0)
    [0.25, "rgba(255, 255, 255, 0)"],  # Fully transparent white (alpha = 0)
    [0.5, "rgba(255, 255, 255, 0.75)"],  # Semi-transparent white (alpha = 0.75)
    [1.0, "rgba(255, 255, 255, 1)"],  # Fully opaque white (alpha = 1)
]

month_order = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]

hour_order = [
    "00:00",
    "01:00",
    "02:00",
    "03:00",
    "04:00",
    "05:00",
    "06:00",
    "07:00",
    "08:00",
    "09:00",
    "10:00",
    "11:00",
    "12:00",
    "13:00",
    "14:00",
    "15:00",
    "16:00",
    "17:00",
    "18:00",
    "19:00",
    "20:00",
    "21:00",
    "22:00",
    "23:00",
]


@dataclass
class PlotParams:
    df: pd.DataFrame
    x_col: str
    y_col: str
    title: str
    x_label: str
    y_label: str
    color_col: Optional[str] = None
    cat_order: Optional[dict] = None


@dataclass
class HeaderAddress:
    zip_5_4: Tuple[int, ...]
    header_line_num: int
    col_names: List[str]


@dataclass
class StartEndDatetime:
    start: pd.Timestamp
    end: pd.Timestamp


@dataclass
class CustomTicks:
    x_ticks: List[str | int | float | pd.Timestamp]
    x_tick_labels: List[str]
    y_ticks: List[str | int | float | pd.Timestamp]
    y_tick_labels: List[str]


def get_zip_header_line_col_names(_file: BinaryIO) -> HeaderAddress:
    logger.info("Reading input file lines and decoding bytes to UTF-8")
    _lines = _file.readlines()
    _lines = [line.decode("utf-8") for line in _lines]

    logger.info("Finding address starting with: Address")
    _address_line_num = next(
        (i for i, line in enumerate(_lines) if line.startswith("Address")), None
    )

    if _address_line_num is None:
        error_msg = (
            "Error: no line starting with Address found. This may be an invalid .csv"
        )
        logger.error(error_msg)
        st.error(error_msg)
        st.stop()

    logger.info("Finding 5+4 zip code in address line")
    try:
        _zip_5_4 = tuple(
            map(int, re.findall(r"(9\d{4})(\d{4})", _lines[_address_line_num])[0])
        )
    except IndexError:
        error_msg = "Error: no zip code found. This may be an invalid .csv"
        logger.error(error_msg)
        st.error(error_msg)
        st.stop()

    # don't need to seek if we assume header comes after address

    logger.info("Finding header, line starting with: TYPE")
    _header_line_num = next(
        (i for i, line in enumerate(_lines) if line.startswith("TYPE")), None
    )

    if _header_line_num is None:
        error_msg = (
            "Error: no line starting with TYPE found. This may be an invalid .csv"
        )
        logger.error(error_msg)
        st.error(error_msg)
        st.stop()

    logger.info(f"Line: {_header_line_num} starts with TYPE and should be the header")

    logger.info("Getting the column names from the header line")
    _column_names = _lines[_header_line_num].split(",")

    return HeaderAddress(_zip_5_4, _header_line_num, _column_names)


def read_process_csv(_file: BinaryIO, _header_address: HeaderAddress) -> pd.DataFrame:
    _df = pd.read_csv(
        _file,
        skiprows=_header_address.header_line_num + 1,
        names=header_address.col_names,
    )

    _df.drop(columns=["TYPE"], inplace=True)
    _df.rename(columns={"USAGE (kWh)": "USAGE"}, inplace=True)
    _df["DATETIME"] = pd.to_datetime(_df["DATE"] + " " + _df["START TIME"])
    _df.set_index("DATETIME", inplace=True)
    _df.COST = _df.COST.apply(lambda x: float(x.replace("$", "")))
    _df["PRICEPERKWH"] = _df.COST / _df.USAGE
    _df["MONTH"] = _df.index.strftime("%b")

    return _df


def get_process_weather(
    _header_address: HeaderAddress, _start_end_datetime: StartEndDatetime
) -> pd.DataFrame:
    _search = SearchEngine()
    _zipcode = _search.by_zipcode(_header_address.zip_5_4[0])
    _location = Point(_zipcode.lat, _zipcode.lng)

    # NOTE: meteostat throws a warning and 2 future warnings
    # FutureWarning: Support for nested sequences for 'parse_dates' in pd.read_csv is deprecated.Combine the desired columns with pd.to_datetime after parsing instead.
    # Warning: Cannot load hourly / 2023 / 74506.csv.gz from https://bulk.meteostat.net/v2/
    # FutureWarning: 'H' is deprecated and will be removed in a future version, please use 'h' instead.
    # NOTE: the second future warning is caused by this meteostat code
    # hourly.py
    #   class Hourly(TimeSeries):
    #     # Default frequency
    #     _freq: str = "1H"

    _weather_hourly = Hourly(
        _location, _start_end_datetime.start, _start_end_datetime.end
    )
    _df = _weather_hourly.fetch()
    _df.index = pd.to_datetime(_df.index, utc=True)
    _df.index = _df.index.tz_convert(local_tz)
    _df["DATE"] = _df.index.date
    _df["TIME"] = _df.index.time
    _df["TEMP_F"] = _df.temp * 9 / 5 + 32

    return _df


def get_heatmap_ticks(_df: pd.DataFrame) -> CustomTicks:
    _first_of_month_cols = [col for col in _df.columns if str(col).endswith("01")]

    _custom_x_ticks = [_df.columns.get_loc(col) for col in _first_of_month_cols]

    _custom_x_tick_labels = [
        col.strftime("%b %Y") for col in _df.columns if str(col).endswith("01")
    ]

    _custom_y_ticks = [i * 4 for i in range(7)]
    _custom_y_tick_labels = [f"{i:02}:00" for i in _custom_y_ticks]

    return CustomTicks(
        _custom_x_ticks,
        _custom_x_tick_labels,
        _custom_y_ticks,
        _custom_y_tick_labels,
    )


def make_heatmaps(
    _weather_df: pd.DataFrame,
    _energy_df: pd.DataFrame,
    _header_address: HeaderAddress,
    _custom_ticks: CustomTicks,
) -> go.Figure:
    _fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        shared_yaxes=True,
        vertical_spacing=0.05,
        subplot_titles=(
            "Hourly energy usage",
            f"Hourly temperature in {_header_address.zip_5_4[0]}",
            "Hourly energy usage (white) over hourly temperature (rainbow)",
        ),
    )
    _fig.update_yaxes(autorange="reversed")

    _colorbar_1_dict = dict(len=0.3, x=1.025, y=0.85, title="kWh")
    _colorbar_2_dict = dict(len=0.3, x=1.025, y=0.5, title="°F")
    _colorbar_3_dict = dict(len=0.3, x=1.025, y=0.15, title="°F")

    _fig.add_trace(
        go.Heatmap(
            x=_energy_df.columns,
            y=_energy_df.index,
            z=_energy_df,
            opacity=1,
            colorscale="portland",
            colorbar=_colorbar_1_dict,
        ),
        row=1,
        col=1,
    )

    _fig.add_trace(
        go.Heatmap(
            x=_weather_df.columns,
            y=_weather_df.index,
            z=_weather_df,
            opacity=1,
            colorscale="portland",
            colorbar=_colorbar_2_dict,
        ),
        row=2,
        col=1,
    )

    _fig.add_trace(
        go.Heatmap(
            x=_energy_df.columns,
            y=_energy_df.index,
            z=_weather_df,
            opacity=1,
            colorscale="portland",
            colorbar=_colorbar_3_dict,
        ),
        row=3,
        col=1,
    )

    _fig.add_trace(
        go.Heatmap(
            x=_energy_df.columns,
            y=_energy_df.index,
            z=_energy_df.to_numpy(),
            opacity=1,
            colorscale=alpha_scale,
            showscale=False,
        ),
        row=3,
        col=1,
    )

    _fig.update_layout(
        title="Heat maps: hourly energy, weather temperature, and high energy on top of weather",
        width=1200,
        height=800,
    )

    _fig.for_each_xaxis(lambda x: x.update(showgrid=False))
    _fig.for_each_yaxis(lambda x: x.update(showgrid=False))

    _fig.for_each_yaxis(lambda x: x.update(tickvals=custom_ticks.y_ticks))
    _fig.for_each_yaxis(lambda x: x.update(ticktext=custom_ticks.y_tick_labels))

    return _fig


def make_line_plot(params: PlotParams) -> go.Figure:
    _mean_value = params.df[params.y_col].mean()

    _fig = px.line(params.df, x=params.x_col, y=params.y_col)
    return _fig


def create_combined_line_plots(*_plot_params) -> go.Figure:
    _subplot = make_subplots(
        rows=len(_plot_params),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        subplot_titles=[p.title for p in _plot_params],
    )

    for i, params in enumerate(_plot_params, start=1):
        plot = make_line_plot(params)
        for trace in plot.data:
            _subplot.add_trace(trace, row=i, col=1)

        line_mean = params.df[params.y_col].mean()

        _subplot.add_shape(
            type="line",
            x0=0,
            x1=1,
            xref="paper",
            y0=line_mean,
            y1=line_mean,
            yref=f"y{i}",
            line=dict(color="red", dash="dash"),
        )

        _subplot.update_yaxes(title_text=params.y_label, row=i, col=1)

    _subplot.update_layout(
        title="Line plots: daily energy and weather temperature",
        height=400 * len(_plot_params),
    )

    _subplot.update_xaxes(showgrid=True)

    return _subplot


def make_bar_char(params: PlotParams) -> go.Figure:
    _fig = px.bar(
        params.df,
        x=params.x_col,
        y=params.y_col,
        color=params.color_col,
        height=400,
        title=params.title,
        color_discrete_sequence=px.colors.qualitative.Light24,
        category_orders=params.cat_order,
    )

    _fig.update_layout(
        title=params.title,
        xaxis_title=params.x_label,
        yaxis_title=params.y_label,
    )

    return _fig


st.markdown("# PG&E *Download My Data* plots")

st.markdown(
    """This tool generates **interactive** plots of:
    
1. [PG&E *Download My Data*](https://www.pge.com/en/save-energy-and-money/energy-usage-and-tips/understand-my-usage.html#accordion-faec0a92be-item-687e81ab07) electric energy usage
2. Hourly weather data at the zip code of the PG&E account via [Meteostat](https://dev.meteostat.net/python/)
"""
)

with st.expander("How to get *your* PG&E data"):
    st.markdown(
        """
To download your PG&E usage data:

1. Open your browser to [https://www.pge.com/](https://www.pge.com/)
"""
    )
    st.image("img/step_1.png")

    st.markdown("2. Enter your login info")
    st.image("img/step_2.png")

    st.markdown("3. Scroll down and click *ENERGY USAGE DETAILS*")
    st.image("img/step_3.png")

    st.markdown("4. Scroll down and click *Green Button / Download my data*")
    st.image("img/step_4.png")

    st.markdown(
        """5. Export the last year of data
    1. Scroll down
    2. Click *Export usage for a range of days* radio button
    3. In the *From* field enter the date exactly 1 year ago (reference the *To* column)
    4. Leave the *To* field set to today's date
    5. Click *EXPORT*"""
    )
    st.image("img/step_5.png")

    st.markdown(
        """6. This should download a .zip file containing two .csv files
We are interested in the one that starts with: `pge_electric_usage`"""
    )
    st.image("img/step_6.png")

    st.markdown("7. Upload the `pge_electric_usage` .csv to the dashboard")


upload_csv = st.file_uploader("Upload your pge_electric_usage ... .csv")

if upload_csv is None:
    logger.info(f"No file uploaded, reading: {filename}")
    try:
        with open(filename, "rb") as file:
            upload_csv = BytesIO(file.read())
            upload_csv.seek(0)
    except Exception as e:
        logger.error(f"Error: could not read: {filename}: {e}")
        st.error(f"Error: could not read: {filename}: {e}")
        st.stop()

if upload_csv is not None:
    header_address = get_zip_header_line_col_names(upload_csv)

    logger.info(f"Column names: {header_address.col_names}")

    logger.info("Rewinding file to process with pandas")
    upload_csv.seek(0)

    logger.info("Read csv to df")
    df = read_process_csv(upload_csv, header_address)

    logger.info("Getting first and last datetimes")
    start_end_datetime = StartEndDatetime(df.index[0], df.index[-1])

    logger.info("Resample energy to daily for line plot")
    daily_electric_df = df.resample("D").sum()

    logger.info("Pivot energy for heat map")
    electric_map_df = df.pivot(index="START TIME", columns="DATE", values="USAGE")

    logger.info(f"Get API weather for zip code: {header_address.zip_5_4[0]}")
    weather_hourly_df = get_process_weather(header_address, start_end_datetime)

    logger.info("De-duplicate weather to accommodate time change")
    weather_hourly_df = weather_hourly_df[
        ~weather_hourly_df.duplicated(subset=["DATE", "TIME"], keep="first")
    ]

    logger.info("Pivot weather for heat map")
    weather_map_df = weather_hourly_df.pivot(
        index="TIME", columns="DATE", values="TEMP_F"
    )

    logger.info("Creating PlotParams instance for daily energy line plot")
    electric_params = PlotParams(
        df=daily_electric_df,
        x_col=daily_electric_df.index,
        y_col="USAGE",
        title="Daily cumulative energy usage (w/ mean)",
        x_label="Date (Pacific time)",
        y_label="Energy (kWh)",
    )

    logger.info("Creating PlotParams instance for daily weather line plot")
    weather_params = PlotParams(
        df=weather_hourly_df,
        x_col=weather_hourly_df.index,
        y_col="TEMP_F",
        title="Daily average temperature (w/ mean)",
        x_label="Date (Pacific time)",
        y_label="Temperature (°F)",
    )

    logger.info("Generating custom x and y ticks for heat maps")
    custom_ticks = get_heatmap_ticks(weather_map_df)

    with st.expander("About heat maps"):
        st.markdown(
            """## Heat maps
    
This is a series of 3 interactive heat maps
    
1. PG&E electric energy usage
    1. Dark blue is lower energy usage
    2. Yellow and eventually red represent higher energy usage
2. Weather data at the zip code of the PG&E account
    1. Dark blue is lower temperature
    2. Yellow and eventually red represent higher temperature
3. Weather data with energy usage overlaid in white
    1. The opacity of the white is proportional to energy usage

The intent is to explore energy usage by time of day, time of year, and compared to the weather temperature

- Each square represents a single hour
    - A column represents a day
    - The whole plot represents a year
- The top of each column is midnight Pacific time
    - The bottom of the plot is 11:00 PM
- Zooming in on any plot will synchronize the zoom window in all 3 heat maps
- A control bar will appear at the top to autoscale or go full screen
- The mouseover hover will show the date and time for each pixel
"""
        )

    logger.info("Plotting heat maps")
    fig = make_heatmaps(weather_map_df, electric_map_df, header_address, custom_ticks)
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("About line plots"):
        st.markdown(
            """## Line plots

This is a series of 2 line plots:

1. PG&E electric energy usage
    1. Daily sum of hourly energy usage
2. Weather data at the zip code of the PG&E account
    1. Daily mean of hourly temperature
3. The yearly mean of each signal is also shown in dashed red

The intent is to explore energy usage by time of year
"""
        )

    logger.info("Plotting line plots")
    fig = create_combined_line_plots(electric_params, weather_params)
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("About bar charts"):
        st.markdown(
            """## Bar charts

This is a series of 2 similar bar charts presenting energy usage by hour and month

1. Hour on the x-axis, where each bar is segmented by month
2. Month on the x-axis, where each bar is segmented by hour

The intent is to explore energy usage by time of year and hour of day
"""
        )

    logger.info("Generating plot_df grouped by hour and month")
    plot_df = df.groupby(["START TIME", "MONTH"]).sum().USAGE
    plot_df = plot_df.reset_index()
    plot_df["MONTH"] = plot_df["MONTH"].astype(str)

    logger.info("Creating PlotParams instance for energy by hour and month")
    cat_order = {"MONTH": month_order}
    plot_params = PlotParams(
        df=plot_df,
        x_col="START TIME",
        y_col="USAGE",
        color_col="MONTH",
        title="Cumulative energy usage by hour and month",
        x_label="Starting hour",
        y_label="Cumulative energy (kWh)",
        cat_order=cat_order,
    )

    logger.info("Making bar chart for energy by hour and month")
    fig = make_bar_char(plot_params)
    st.plotly_chart(fig, use_container_width=True)

    logger.info("Creating PlotParams instance for energy by month and hour")
    cat_order = {"MONTH": month_order, "START TIME": hour_order}
    plot_params = PlotParams(
        df=plot_df,
        x_col="MONTH",
        y_col="USAGE",
        color_col="START TIME",
        title="Cumulative energy usage by month and hour",
        x_label="Month",
        y_label="Cumulative energy (kWh)",
        cat_order=cat_order,
    )

    logger.info("Making bar chart for energy by month and hour")
    fig = make_bar_char(plot_params)
    st.plotly_chart(fig, use_container_width=True)
